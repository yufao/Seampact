# CRAG 原生 SDK 验证

截至 2026-10-12，`probe_crag_sdk.py` 的 9 个原序／适配配对案例通过。测试使用隔离 CPU 环境中的 `llama-index-core==0.14.25`、`llama-index-workflows==2.22.2`，本次 Pydantic 版本为 2.14.1。

冻结输入为 LlamaIndex revision `6dd2f3cdd9ce7ff927ea8d46ccee08875bb00f95` 的 `corrective_rag_pack.ipynb`。探针执行原 cell 8 的事件定义和原 cell 10 的完整 Workflow 类：所有 7 个方法的 `@step`、签名、原提示词和原控制流都保留。适配版仅使用引擎生成、通过逆向 AST 核验的顺序循环体提取，再安装每次调用的许可门禁。

模型、搜索、向量索引和摘要查询四类外部端点使用受控测试实现。Workflow SDK、Context.store、事件验证、Document、NodeWithScore、PromptTemplate 都使用真实已安装的类。探针跳过 notebook 中的下载、安装、密钥设置等其他 cell，并显式提供 SDK／测试端点命名空间。

9 个配对案例覆盖：单步空输入、单步执行中更换 Context 内的模型、单步原异常、单步原取消；真实 SDK 的完整查询路径中全相关、混合结果触发搜索、空检索、原异常；以及真实 SDK 的 ingest 分支。原序与适配版比较最终值、Context 保存值、外部端点调用顺序及 `@step` 接口。适配版还检查未获许可时不调用原端点、每次门禁只启动一次、异常／取消后退休及执行槽归零。更换模型的案例确认原循环仍逐项读取 Context 内的模型。

## 事件列表的实际复制行为

真实 SDK 验证发现：原 `RelevanceEvalEvent` 的列表字段经过 Pydantic 验证后，`event.relevant_results is saved_results` 为 `False`，而两者的值相等。独立的事件构造检查还确认：事件列表与传入列表不是同一对象，随后向传入列表追加元素不会改变事件列表。原序与适配版的结果一致，报告保存了 `event_list_aliases_store=false` 和 `event_constructor_semantics`，把事件构造本身的复制行为与 Context 读写分开核验。

因此，提取循环体时“把原列表作为参数传入”只能保证该局部调用边界保留引用。后续经过原生事件构造、验证或序列化边界，是否保留别名由原框架决定，必须跟随该边界的实际语义。此前片段探针用简化的事件工厂观察到同一列表对象，仅适用于该受控片段，不能推广到真实事件边界。引擎不应为了统一接口强行改变原框架的复制行为。

## 证据范围与复现产物

这些案例证明固定输入、固定 SDK 版本和受控端点下，生成的顺序提取产物能在原查询控制流中等待许可并继续。候选边界由探针手选；它们不构成自动发现或任意拆分的证明。原始模型与检索质量为 `NOT_RUN`，完整依赖／外部效果闭包为 `UNKNOWN`，并行与生产准入均为 `false`。完整查询控制流通过也不代表原 OpenAI、Tavily、向量检索或摘要后端已经接入。

运行产物保存在调用者指定的本地输出目录，按需SFTP交付，不进入Git：

- `crag-sdk-result.json`：9 个配对结果、SDK 版本、原源码摘要、转换核验和范围说明。
- `crag-sdk-environment.json`：本地隔离环境的依赖版本及依赖一致性检查。
- `crag-sdk-sequential-draft.zip` 与 `.sha256`：实际用于测试的生成包；包摘要与结果报告相同。包内同时保留原 source、独立 patched、adapter 和核验材料。

可选依赖见 [requirements-sdk-crag.txt](requirements-sdk-crag.txt)。工具沿用两个位置参数：源码根目录、结果输出目录。容器中的可选 CPU SDK profile 可以运行同一工具，服务器／容器执行结果需另行记录，不能用本地通过代替。
