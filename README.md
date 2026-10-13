# SeamPact

异构 Harness 接入与任务工作台。当前 DEMO 主线是 **来源 → API 自动分析 → 能力包 → CRAG 文档问答 → 答案与调用轨迹**。

源码、配置模板、来源清单和有效设计由 GitHub 管理；模型、数据、密钥、第三方快照、API 对话、数据库和运行产物留在 Git 忽略目录，按需通过 SFTP 交付。

## 快速启动

```bash
git clone git@github.com:yufao/Seampact.git
cd Seampact
python -B tools/fetch_sources.py
python -m venv .venv
```

Linux 激活 `.venv/bin/activate`，Windows PowerShell 激活 `.venv/Scripts/Activate.ps1`。在这个项目环境安装 CRAG 运行依赖，再启动工作台：

```bash
python -m pip install -r docs/requirements-sdk-crag.txt
python -m pip install -e .
python -B -m seampact serve --home var/platform --sources sources.json
```

打开 http://127.0.0.1:8874 。Linux 使用终端显示的本次登录链接；服务器通过 SSH 端口转发访问。[服务器 Docker 启动](docs/Docker部署.md)只构建并启动一个 DEMO 服务，不要求先跑实验矩阵。

## 在页面里完成一个任务

1. 在接口设置中配置 DeepSeek 或 GLM。Windows Key 使用当前用户 DPAPI；Linux 使用项目私有环境／秘密文件，见[服务器说明](docs/服务器部署.md)。费用、次数和分析轮次不设应用上限，保留完整上下文与提供方最大输出设置。
2. 选择 CRAG，写下希望提取的能力和任务要求，创建来源快照并启动 API 分析。
3. 分析完成后生成能力包。引擎独立检查源码锚点与支持的接线／局部重构；页面显示实际生成的能力。
4. 选择这个包，输入问题和文本资料，选择真实模型运行。得到答案、实际激活的能力、模型调用用量和执行轨迹。

也可使用以前真实 API 生成的 CRAG 包：将原包 ZIP 与 `crag-text.json` 放进私有 `artifacts/`，启动时添加 `--artifacts artifacts`。页面明确显示“已有 API 能力包”；新生成的包不会被替换。受控预览是独立可选模式，不发送模型请求。

当前 CRAG 任务保留冻结版本的原 Workflow 方法与事件控制，采用用户提供资料的本地文本检索／搜索端点，以及实际 DeepSeek／GLM 调用。支持的局部循环可通过生成 adapter 门控。搜索范围仅是输入资料；尚未接入在线搜索或完整向量模型。SDK 的无效果 StartEvent 分支与真正处理资料的能力分开记录。

## 代码与交付

| 目录 | 内容 |
|---|---|
| `src/seampact/` | 来源分析、包生成、运行门控、HTTP 服务和原生前端 |
| `tools/` | 固定来源获取、CRAG 任务宿主及可选研究工具 |
| `config/source-spec.json` | 四源固定修订、选定工件和摘要；不包含第三方源码 |
| `deploy/` | 单 DEMO 服务及可选实验 profile |
| `docs/` | 当前设计、任务运行和部署说明 |
| `sources/`、`var/`、`artifacts/` | 本地生成／下载内容，Git 忽略 |

四个来源可以登记和分析；当前可交互任务宿主支持指定修订的 CRAG 包。MMCT、ABC、DiSP 的真实任务接入继续实现。调度目前释放原控制流已经启用的调用，采用 FIFO；并行、跨源组合、K8S 和两层 RL 为后续功能。

[设计与实现路线](docs/设计与实现路线.md) · [原生接线与有限重构](docs/原生接线与有限重构.md) · [任务 DEMO](docs/任务DEMO.md)
