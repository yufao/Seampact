# Docker 部署与网络选择

更新：2026-10-15，SeamPact 0.8.0。完整远端命令从[服务器部署](服务器部署.md)开始；本页解释镜像、路径、host／Compose 两条路线与依赖。二者选择一条，不同时读写同一状态目录。

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

镜像标签使用实际commit，避免同一个0.8.0标签掩盖不同代码。镜像仍是Python3.11，版本不是由宿主系统Python决定。系统daemon的平台容器使用宿主实际UID／GID，rootless使用容器0:0；完整构建与run命令见服务器指南第3节。Host网络依据[Docker官方文档](https://docs.docker.com/engine/network/drivers/host/)，本指南面向Linux，不要求修改Windows Docker Desktop设置。

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

代码中的固定image标签为seampact-core:0.8.0；更新用 `--build` 和 `--force-recreate` 重建／替换该Compose项目，不能只pull代码就认为进程已经更新。Host主路线用commit标签则更容易识别实际版本。

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

2026-10-20 新设计见[服务与权重资源配置](模型服务与权重资源配置设计.md)，命名连接和权重表单仍待实现。Linux 完整凭据路径按私有目录0700、文件及临时文件0600处理，按实际映射 UID／GID 核对；这不是磁盘加密。资源路径采用容器内路径，宿主权重需由用户按现有挂载方式提供，平台不自动搬运文件。环境变量只属于已启动的容器进程，宿主后来修改不自动改变旧运行。

公开Git URL可直接接入；私有源不自动继承宿主SSH凭据，可先由宿主取得后挂载。路径使用容器实际视角，不填电脑Windows路径。模型和数据保留私有挂载，不放进Git或核心镜像。

核心镜像仅有平台、Python3.11、Git和证书，不是所有Harness依赖环境。运行原Harness时仍需要其实际依赖。适配哪套原依赖应由具体入口和原要求确定，在专属镜像／虚拟环境安装；不向宿主系统Python执行全局pip，不随意改来源的依赖版本。依赖文件引用其他文件、源码包或本地扩展时，应保留原构建结构，不能只复制一个requirements文件就声称完整环境。

Host平台可以连接宿主上127.0.0.1的模型API；bridge中的127.0.0.1只指容器自身。bridge模型服务需本就可达的容器／网络地址，不能仅换成host-gateway就假定原回环服务可达。

生成的worker镜像只支持当前已核对的有限同步标量入口；它没有下载模型、申请GPU或安装原SDK。数据／对象远程条件未覆盖时保留本地。当前托管配额不是共享服务器的GPU显存分配器。

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
