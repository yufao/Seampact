# 已停用的人工接入材料（仅保留历史）

2026-10-19 用户纠正：本目录中的入口由研究 Agent 手写，提前替平台完成了构造和接线。即使随后交给分析器检查，也不符合“从原始仓库自动分析、拆分与重组接入”的任务目标。此前将它们作为当前接入路径是范围判断错误。

2026-10-19 当时的 REX、QACD、PaperGraph 手工包装来源已从当时能力池摘除，历史分析和真实运行保留；这不代表之后重新分析的原库来源也已摘除。**不要再把下列文件复制进来源或作为分析器输入；下列操作仅说明历史实际做过什么。** 基于它们得到的 30 个运行归为人工辅助工程探索，不能作为 SeamPact 自动接入或完整机制有效性证据。独立原始提交副本与新分析记录位于 `var/prototype/research/original-intake/`；新输入不含手写包装、指定入口或预填拆分方案。

- `rex_entry.py` 曾复制到 REX-RAG 根目录为 `seampact_entry.py`，人工构造原 Config／BM25Retriever 并绑定索引和语料。
- `qacd_entry.py` 曾复制到 QACD 根目录为 `seampact_entry.py`，人工提供 provider 与 scorer 构造接线。
- `papergraph_entry.py` 曾复制到 PaperGraphBench 的 `papergraph_demo/` 为 `seampact_entry.py`，人工构造客户端及完整／细入口。
- `capabilities.json` 是人工复核能力卡，也不属于分析器自动输出；保留供历史运行解释。

这些手工入口只解释旧运行，不作为新接入输入。当前主线是完整仓库的自动能力重构，平台生成物与外部人工适配分别记录；接入保证按实际转换声明，见 [平台设计](../../docs/设计与实现/Harness接入与调度重设计讨论.md)。

本次 REX 环境使用 Java 21、工作区内的 `HF_DATASETS_CACHE` 和 Pyserini 1.6.0。该版本在导入未使用的 OpenAI encoder 时会构造 SDK client；本次进程设置了明确的非密钥占位 `OPENAI_API_KEY=UNUSED_BM25_NO_OPENAI_REQUESTS` 以满足其导入要求，实际 BM25 分支不发送模型请求。它不是可调用的模型凭据。这是该历史运行的环境记录；如需复现，先核对固定版本和实际资源条件。其他模型路径仍需各自真实资源。

固定来源、真实配置、运行结果及仍缺资源见 [四候选接入与真实实验](../../docs/history/四候选接入与真实实验.md)。
