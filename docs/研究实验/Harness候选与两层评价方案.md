# Harness 候选与能力评价

更新：2026-11-11。下表是完整来源库的候选，未据此声明已经接入0.16。任务／攻击基准见[数据源选型](Agent任务与攻击数据源选型.md)，与防护库分别计数。

## 候选来源

| 来源 | 可抽取的用途与挂载位置 | 资源和重叠 |
|---|---|---|
| [LLM Guard](https://github.com/protectai/llm-guard) | 输入／输出中的注入、敏感内容等具体scanner | 规则CPU或模型CPU／GPU；按选定scanner描述，与Pytector／Presidio有重叠 |
| [Presidio](https://microsoft.github.io/presidio/) | PII识别及匿名化，材料进入上下文或对外交付前 | CPU及NLP模型，特定transformer可GPU；必须将替换真正接到宿主 |
| [LlamaFirewall](https://github.com/meta-llama/PurpleLlama/tree/main/LlamaFirewall) | 注入、Agent对齐与代码检查的具体实现 | 本地授权权重、模型API或静态分析；部分检查需要推理上下文，不能假定外部模型提供 |
| [Pytector](https://github.com/MaxMLang/pytector) | 文本注入检测及选定净化方法 | 依版本采用规则／本地模型／API；与LLM Guard的同类模型去重 |
| [NeMo Guardrails](https://docs.nvidia.com/nemo/guardrails/about-nemo-guardrails-library/rail-types) | 提取具体输入、检索、工具或输出rail算法 | 资源依具体方法；框架本身不再作为第二个Agent宿主，不与内嵌相同scanner重复计能力 |
| [Guardrails AI](https://github.com/guardrails-ai/guardrails) | 指定validator的结构、内容和输入输出验证 | CPU／模型／API随validator；Hub独立来源和许可单列，不能只包框架便计完整防护 |
| [RefChecker](https://github.com/amazon-science/RefChecker) | 有参考材料的主张核验 | 抽取模型／API和NLI模型，按实际实现CPU／GPU；完整核验优先，独立NLI须有资源收益 |
| [SelfCheckGPT](https://github.com/potsawee/selfcheckgpt) | 事实型回答的多次采样一致性 | 额外生成与检查成本；一致性不等于真值，不能代替任务原评分 |
| [Semgrep](https://github.com/semgrep/semgrep) | 代码／补丁中的指定规则扫描 | CPU和规则；适用于真实代码任务，规则版本与许可固定 |
| [Bandit](https://github.com/PyCQA/bandit) | Python代码安全AST检查 | CPU；与Semgrep部分重叠，作为替代或补充需明确任务 |
| [detect-secrets](https://github.com/Yelp/detect-secrets) | 代码／文件中的密钥痕迹扫描 | CPU规则与熵；检测结果由应用决定阻断或提示，不能自动宣称识别所有秘密 |
| [TruFor](https://github.com/grip-unina/TruFor) | 有图像任务的篡改定位和完整性分析 | 本地权重CPU／GPU；不用于纯文本请求，不等于所有图像真实性判断 |
| [Haystack](https://github.com/deepset-ai/haystack) | 业务RAG：关键词／向量检索及本地重排，按需读取授权知识资料 | CPU索引、embedding／reranker；接入检索组件，不增加Agent宿主，明确计作业务增强来源 |

[REX-RAG](https://github.com/MiliLab/REX-RAG)可作业务检索来源，不默认计入防护库。[JailbreakEval](https://github.com/CryptoAILab/JailbreakEval)是响应评价器集合，默认属于评分研究；不能未经任务定义便当在线防护或AgentDojo原判分。来源的模型API、权重许可和依赖按实际固定版本登记，表中资源类型不是本机测量结果。

## 场景组合

办公优先考虑文本注入、隐私、外发／操作、内容与证据检查；软件加入Semgrep、Bandit和detect-secrets；资料场景增加RefChecker／SelfCheckGPT，有真实图像任务时才加入TruFor。NeMo和Guardrails只选择需要的具体方法，不复制其完整宿主。

每场目标七至十个来源库。平台先为每个候选列出真实用途、适用任务、输入输出、原实现、资源所有者、可用干预及效果评价。根据任务覆盖和资源异质性选择组合；一个源库多个scanner不算多个来源，同一底层检测器不同框架包装不能伪装成不同防护。候选尚需去重和实际接入，当前不宣称任何场景已经满足数量目标。

办公首批方案现为七个防护来源加一个RAG来源，共八库，见[办公协作方案](办公协作场景方案.md)。基础宿主、业务增强、防护和评分角色分开记录；不将RAG计成额外防护算法，也不排除它参与能力和实例调度。

## 两种评价

接入评价记录从完整库到可执行能力的自动化程度、真正缺口及必要人工改动，检查相关原例子和任务行为；来源引用、接口和首调分别提供有限依据。标准重构不自动继承原库语义等价结论。

业务评价在同一Agent的完整任务中进行，使用来源原生任务／攻击指标及实际成本。被调度的防护输出不能充当自己的金标。正常任务也可需要检索和核验，算法调用成功与任务完成、防护有效分别记录。
