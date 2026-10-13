# Git 管理的 DEMO 部署

服务器更新代码使用 Git，运行数据使用项目私有目录。默认只构建并启动 `seampact-demo` 项目的 `control` 服务，镜像带 CRAG SDK；无需先执行探针矩阵。复用服务器已安装的 Docker，不修改共享环境或用户组。

## 首次启动

以自己的普通账户执行；sudo 仅用于已获准的系统 Docker。项目运行目录由普通账户创建，容器沿用其 UID／GID。

```bash
git clone git@github.com:yufao/Seampact.git
cd Seampact
python3 -I -B tools/fetch_sources.py
python3 -I -B deploy/prepare_workspace.py "$PWD" "$HOME/seampact-demo-runtime" --port 8875
sudo systemctl start docker
sudo docker compose --env-file "$HOME/seampact-demo-runtime/compose.env" -f deploy/compose.yaml up -d --build control
sudo docker compose --env-file "$HOME/seampact-demo-runtime/compose.env" -f deploy/compose.yaml logs --tail 12 control
```

首次构建下载固定 Python 基础镜像和 CPU SDK 包，没有模型或 GPU 依赖。`fetch_sources.py`只下载来源清单中的29个小工件，核对固定修订／摘要；生成的本地构建清单描述当前代码，不是沿用旧发布的验收记录。

端口仅发布服务器回环地址8875。SSH本地转发本地8875→服务器127.0.0.1:8875，打开本次登录链接。日志含登录令牌，只在自己的终端查看。

## 配置接口

```bash
python3 -I -B deploy/write_secret.py "$HOME/seampact-demo-runtime/secrets" deepseek
sudo docker compose --env-file "$HOME/seampact-demo-runtime/compose.env" -f deploy/compose.yaml up -d --no-build --no-deps --force-recreate control
```

Key隐藏输入；GLM可用同一助手后补。只在本平台没有活动任务时重建控制容器。页面里选择型号和发现能力，再开始分析或真实任务。额度与上下文规则沿用平台设置，付费失败不自动重放。

## 更新

```bash
git pull --ff-only
python3 -I -B tools/fetch_sources.py
sudo docker compose --env-file "$HOME/seampact-demo-runtime/compose.env" -f deploy/compose.yaml up -d --build control
```

源码与下载内容分开；已验证的第三方文件直接复用。新 Git 路径下的控制镜像含最新页面和任务宿主，SDK依赖层可缓存。已有项目或端口属于其他工作区时，先返回实际情况，调整本项目名称／端口，不停止其他工作。

## 按需 SFTP

模型、数据、秘密配置与旧 API 能力包不进入 Git。若需要复用已有 API 包，将 ZIP 与记录放进运行目录的 `state/artifacts/`；容器将其读取为 `/data/artifacts`。新包可直接由当前页面生成，不要求额外 SFTP。数据库、完整模型消息和任务输入位于 `state/platform/`，报告位于 `reports/`。

实验 profile 与 `tools/verify_docker.py`作为研究工具保留，按具体问题调用。它们不阻塞默认 DEMO 的启动或任务开发。
