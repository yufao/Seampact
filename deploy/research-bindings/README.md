# 已停用的人工接入材料（仅保留历史）

2026-10-19 用户纠正：本目录中的入口由研究 Agent 手写，提前替平台完成了构造和接线。即使随后交给分析器检查，也不符合“从原始仓库自动分析、拆分与重组接入”的任务目标。此前将它们作为当前接入路径是范围判断错误。

相应 REX、QACD、PaperGraph 来源已从当前池摘除，历史分析和真实运行保留。**不要再把下列文件复制进来源或作为分析器输入；下列操作仅说明历史实际做过什么。** 基于它们得到的 30 个运行归为人工辅助工程探索，不能作为 SeamPact 自动接入或完整机制有效性证据。独立原始提交副本与新分析记录位于 `var/prototype/research/original-intake/`；新输入不含手写包装、指定入口或预填拆分方案。

- `rex_entry.py` 曾复制到 REX-RAG 根目录为 `seampact_entry.py`，人工构造原 Config／BM25Retriever 并绑定索引和语料。
- `qacd_entry.py` 曾复制到 QACD 根目录为 `seampact_entry.py`，人工提供 provider 与 scorer 构造接线。
- `papergraph_entry.py` 曾复制到 PaperGraphBench 的 `papergraph_demo/` 为 `seampact_entry.py`，人工构造客户端及完整／细入口。
- `capabilities.json` 是人工复核能力卡，也不属于分析器自动输出；保留供历史运行解释。

当前流程只向平台提交完整原始仓库地址、固定版本和真实外部资源，由分析器自行发现入口与边界，由既有引擎核对。缺少合法构造／原 owner 生命周期绑定时保持 MISSING，不能再手写来源专用入口消除缺口。教师标准仍由用户在前端填写。

本次 REX 环境使用 Java 21、工作区内的 `HF_DATASETS_CACHE` 和 Pyserini 1.6.0。该版本在导入未使用的 OpenAI encoder 时会构造 SDK client；本次进程设置了明确的非密钥占位 `OPENAI_API_KEY=UNUSED_BM25_NO_OPENAI_REQUESTS` 以满足其导入要求，实际 BM25 分支不发送模型请求。它不是可调用的模型凭据。重新启动同一实验时需保留这些环境条件；其他模型路径仍需各自真实资源。

固定来源、真实配置、运行结果及仍缺资源见 [四候选接入与真实实验](../../docs/四候选接入与真实实验.md)。
