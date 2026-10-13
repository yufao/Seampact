# SeamPact

0.6.0：异构 Harness 自动接入、有限拆分与两层 RL 调度的学术原型。首页是正常 AI 对话；配置中填写 Harness 仓库 URL／本地目录，以及独立的对话模型和分析模型。右侧显示实际能力池、分析结果、Unit 调用与调度状态。

核心链路是 **地址 → 全目录解析 → API 工具分析 → 源码检查与原生绑定 → Unit 能力池 → 对话提出可执行方案 → 两层 RL → 原 Harness 执行 → 对话结果**。没有可用 Unit 时，仍可正常对话。

## 启动

平台核心只需 Python 3.11 或以上；接入远程仓库还需 Git。Harness 自身的依赖需要装在实际运行它的 Python 环境中。

Linux，在仓库根目录执行：

```bash
PYTHONPATH=src python -B -m seampact serve --home var/prototype --port 8880
```

Windows PowerShell：

```powershell
$env:PYTHONPATH = 'src'
python -B -m seampact serve --home var/prototype --port 8880
```

打开 http://127.0.0.1:8880。配置两个模型角色，保存后直接发送消息。对话模型支持本地或云端的 OpenAI 兼容接口；分析模型使用云端接口。接口地址填基础路径，如本地 `/v1`；平台追加 `/chat/completions`。

接入 Harness 后，在配置中点击“分析”。分析器使用通用源码工具读取整仓，并按确认的 RuleSet 提出入口、初始化、资源绑定及 W／H／R 边界。引擎核对实际源码和支持规则，结果进入能力池。`READY` 表示静态绑定准备完成，原生初始化在真正调用时发生；缺依赖或参数的 Unit 保留为 `MISSING`，显示具体原因。

## 当前实现范围

| 层 | 实际功能 |
|---|---|
| 前端 | 标准对话、模型与 Harness 配置、实际运行观察 |
| 分析器 | URL／目录接入，全目录清单，工具分析，源码锚点检查，原生入口与有限 W／H／R 接线 |
| 调度器 | 上层选择同一要求下的可执行完整方案；下层选择原 controller 已使能、owner 与资源条件允许的调用；两层在线表格 Q-learning |
| 模型适配 | 对话角色支持本地／云端 API；分析角色支持云端 API；独立配置，保留完整上下文，无应用费用或轮次上限 |

支持的是有限原生绑定，并非任意仓库均可执行。复杂 Model／Context 参数、动态 SDK 接线、生成器和未满足的资源条件会明确报缺失。原 controller 与 owner 保留；单个方案按步骤顺序执行；未开放模型自述的并行或远程派发。初版奖励使用调用成功和时延，不代表已衡量最终答案质量或证明 RL 效果。

## 文档与部署

[手动启动与演示](docs/手动启动与演示.md) · [当前实现](docs/当前实现.md) · [设计与实现路线](docs/设计与实现路线.md) · [原生接线与有限重构](docs/原生接线与有限重构.md) · [组会报告](docs/组会报告.md) · [Docker 部署](docs/Docker部署.md)

`src/seampact/` 是四层核心实现；`deploy/` 是一个平台容器。代码与当前文档由 [GitHub](https://github.com/yufao/Seampact) 管理；模型、数据、密钥、下载的第三方仓库和 `var/` 运行状态保留本地，按需 SFTP。旧 0.5 文档分类归档于 `docs/历史草案/0.5/`。
