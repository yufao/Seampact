# 单服务 Docker 部署

更新：2026-10-13，0.6.0。镜像只包含平台核心、Python 3.11 和 URL 接入所需 Git。没有预制样例、来源专用依赖或额外工作流容器。

## 在 Linux 仓库目录启动

确认使用的是当前 Git 仓库，而不是旧上传包的 `releases/seampact-core-0.4.0`。在仓库根目录执行：

```bash
mkdir -p var/prototype harnesses
export SEAMPACT_UID="$(id -u)"
export SEAMPACT_GID="$(id -g)"
export SEAMPACT_PORT=8880
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml logs --tail 80 platform
```

该命令只创建 `seampact-prototype` 项目中的一个 `platform` 服务。Compose 使用当前用户 UID／GID写入状态目录；不会安装服务器系统 Python 的包。

宿主映射为 `127.0.0.1:8880`，容器内部平台监听 `0.0.0.0:8880`。通过 Terminus 的 SSH 本地端口转发打开本机 http://127.0.0.1:8880。转发远端主机 `127.0.0.1`、远端端口 `8880`；若本机端口占用，可转发至另一个本地端口。

## 私有目录和模型配置

| 宿主位置 | 容器位置 | 内容 |
|---|---|---|
| `var/prototype` | `/data` | 接口配置、私有密钥、状态数据库、冻结工件、远程仓库与 Q 表 |
| `harnesses` | `/harnesses` | 用户通过 SFTP 放入的项目专用 Harness／配置／数据目录 |

页面地址应使用 `/harnesses/my-harness`，不能填写服务器宿主的 `/home/...` 路径。URL 可以直接填写远程 Git HTTPS 地址；私有 Git SSH 连接需要容器中实际可用的 Git 凭据，平台不自动复制服务器 SSH 密钥。

在页面配置 DeepSeek／GLM 等云端模型。密钥保存在 `/data` 私有配置中；不需要为 Docker 再创建旧的 secret handoff 文件。也可以使用 `.env.example` 中列出的环境变量，通过私有文件和 `--env-file` 传给 Compose。

如需改变宿主挂载目录：

```bash
export SEAMPACT_DATA_DIR="$HOME/seampact-workspace/platform-state"
export SEAMPACT_HARNESS_DIR="$HOME/seampact-workspace/harnesses"
mkdir -p "$SEAMPACT_DATA_DIR" "$SEAMPACT_HARNESS_DIR"
docker compose -f deploy/compose.yaml up -d --build
```

容器对话模型连接本地模型服务时，`127.0.0.1` 指容器本身。填写容器实际可达的模型地址；模型服务在另一个容器时需要可达的网络地址，在宿主时需要宿主可达地址。

## Harness 自身的依赖

核心镜像不包含任何特定 Harness 的运行库。若某入口需要第三方包，它会显示 `MISSING`，直至在实际运行环境中提供依赖。

最直接的做法是用一个独立虚拟环境原生启动平台，并在同一环境安装该 Harness 的已有依赖，见[服务器部署](服务器部署.md)。若选择 Docker，可按接入仓库自己的依赖文件扩展这个 Dockerfile，并重新构建。用户数据和模型权重保持挂载；不要把它们放进 Git 或平台核心镜像。

分析过程只读取来源。真实调用时，原 Harness 可能按原算法写入该项目专用目录，因此保留可写挂载；将输出参数指向自己的项目目录，不挂载其他用户的数据目录。

## 更新或停止

更新 Git 后仍在相同仓库目录执行 `docker compose -f deploy/compose.yaml up -d --build`。停止只使用：

```bash
docker compose -f deploy/compose.yaml down
```

该命令针对当前单服务 Compose 项目，不清理其他 Docker／KIND 项目，也不删除宿主的私有挂载目录。
