# Agent 任务与攻击数据源选型

检索核查：2026-11-11。共比较25组常用或直接相关来源，下面给出原始项目、原评分和接入取舍。表中优先级是对本研究首次接入成本的判断，未下载、运行或测得工时。推荐组合见末节，完整协议见[混合负载](多来源共享资源场景协议.md)。

实验中的业务执行走同一 Agent；来源提供任务、工具环境和评分。奖励用于训练，成功与攻击成功依据原评分器。程序判定、答案匹配、模型裁判分别标明，不把“有指标”误写成全部都是确定性判定。

## 正常 Agent 能力：13组来源

| 来源与版本依据 | 任务及原评价 | 环境、访问与取舍 |
|---|---|---|
| [AgentBench](https://github.com/THUDM/AgentBench)，2023／逐域固定 | OS、DB等八交互域；各域成功、F1或进度指标不同 | 多轮环境与Docker依赖按域配置；Apache-2.0代码。选域补工具任务，不把游戏奖励通称业务成功 |
| [GAIA](https://huggingface.co/datasets/gaia-benchmark/GAIA)，固定数据快照 | 附件、浏览、计算等资料任务；官方最终答案规则匹配，题集本身不提供统一宿主 | HF门控条款、测试答案隐藏，禁止公开重分发；2025-10改Parquet。已获数据访问时用于资料场景，今晚不依赖新审批 |
| [τ²-bench](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md)，当前仓库已演进到τ³ | 零售／航旅等业务协作；终态数据库和必要沟通，后者可用语义模型裁判 | 有用户模拟器与额外模型费用；MIT代码。任务、reward_basis与版本固定；旧[τ-bench](https://github.com/sierra-research/tau-bench)已提示任务过时。适合业务协作备选，不直接称邮件办公 |
| [BFCL](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard)，V4分类固定 | 工具名／参数、单轮AST与多轮状态／执行检查；类别协议不同 | 适合工具正确性补充；单轮条目不能独自证明完整任务完成。实时API类另有网络条件 |
| [ToolBench](https://github.com/OpenBMB/ToolBench)／[StableToolBench](https://github.com/THUNLP-MT/StableToolBench) | API组合任务；完成与胜率依赖模型裁判，后者以缓存和模拟改善工具稳定性 | 原API可能失效，模拟工具需显式标注；API／裁判／缓存有成本。后延，不优先作真实环境终态依据 |
| [SWE-bench Verified](https://github.com/SWE-bench/SWE-bench) | 真实issue修复，生成补丁，官方容器运行FAIL_TO_PASS及PASS_TO_PASS测试 | Docker／依赖和长轨迹成本；仅覆盖软件工作台的修复子集。固定版本与任务ID；测试是原评分标准，不是所有软件行为正确性的证明 |
| [SWE-bench Pro](https://github.com/scaleapi/SWE-bench_Pro-os)，V2 | 长程开发任务、补丁测试；当前V2与旧v1任务集不同 | V2默认642题；离线agent执行和新容器复评分有明确要求。镜像与任务较重，后延；加入外网或攻击改造不能混称原协议 |
| [WebArena](https://github.com/web-arena-x/webarena)，任务／镜像版本固定 | 购物、论坛、GitLab等浏览器多步任务；程序检查页面／环境状态及答案 | 自托管网站Docker较重，代码Apache-2.0，站点及数据权利另查。环境已备好时扩展，今晚不新搭整站 |
| [WorkArena／WorkArena++](https://github.com/ServiceNow/WorkArena) | ServiceNow知识工作、工单等；程序验证站内目标状态 | 当前官方实例访问受门控，需申请条款审批；后延。BrowserGym是[环境接入框架](https://github.com/ServiceNow/BrowserGym)，不另计一个数据集 |
| [OSWorld](https://github.com/xlang-ai/OSWorld) | 办公软件／跨应用桌面操作；程序检查文件和环境 | VM／图形桌面；若采用截图观测需视觉模型，部分任务需要账户或网络，首次部署重；后延桌面场景 |
| [AssistantBench](https://assistantbench.github.io/)，EMNLP 2024 | 多网页信息搜集、整理回答；原答案评价含accuracy、precision等 | 开发集可评分、测试答案隐藏，网页可变化；资料场景优先备选。保留搜索／浏览条件，不能只提供答案上下文称完整浏览任务 |
| [SpreadsheetBench](https://github.com/RUCKBReasoning/SpreadsheetBench)，V1／Verified固定 | 真实工作簿修改；原OJ对多个输入／参考结果检查，公式需重算 | Excel／LibreOffice及文件环境；项目注明CC BY-SA 4.0。办公文件优先；V2另有工作流，不能与V1混名 |
| [BrowseComp](https://openai.com/index/browsecomp/)，[原评测实现](https://github.com/openai/simple-evals/blob/main/browsecomp_eval.py) | 困难网页信息定位；参考答案加模型裁判 | 搜索／浏览和裁判费用，页面实时变化；资料高难补充，今晚后延。最终答案指标不直接覆盖文件和操作效果 |

根仓库许可不自动覆盖所有模型、网页、任务数据或衍生环境；执行清单记录实际使用组件和许可。官方测试集即使可见答案也不因此变成训练集。

## 攻击与安全：12组来源

| 来源与版本依据 | 威胁、环境及原评分 | 使用位置与限制 |
|---|---|---|
| [AgentDojo](https://github.com/ethz-spylab/agentdojo)，[论文v3](https://arxiv.org/html/2406.13352v3) | 工具返回注入；Workspace／Slack／Travel／Banking，有状态模拟Python工具；程序utility检查正常任务，security检查攻击目标 | 首选完整任务主干。clean utility、attacked utility、targeted ASR分开；本地工具环境不需真实银行或办公账户。MIT，锁定commit |
| [AgentDyn](https://github.com/SaFo-Lab/AgentDyn)，[论文v2](https://arxiv.org/html/2602.03117v2) | 动态跨应用任务、工具注入及必须遵从的合法外部指令；Shopping／GitHub／DailyLife；程序检查状态及部分轨迹 | 首选软件／动态协作；适合观察过度防护损害正常业务。MIT，沿用Dojo框架；仓库保留Dojo旧套件，必须去重 |
| [AgentHarm](https://github.com/UKGovernmentBEIS/inspect_evals/tree/main/src/inspect_evals/agentharm)，[论文v3](https://arxiv.org/html/2410.09024v3) | 恶意用户直接申请多步行为；合成工具；人工编写的工具／参数完成条件，部分语义及拒绝使用LLM judge | 直接恶意请求核心补充；benign是同主题改写对照，不是自然日常流量。MIT加安全研究用途条款，固定公开split与实现commit |
| [Agent-SafetyBench](https://github.com/thu-coai/Agent-SafetyBench)，[论文v2](https://arxiv.org/html/2412.14470v2) | 敏感信息、参数、确认、工具结果过信等多类安全交互；主指标ShieldAgent的safe／unsafe判断 | 广义风险补充；本地7B judge或服务有成本。论文helpfulness是允许部分帮助的模型判定，不等于确定性任务完成 |
| [ASB／Agent Security Bench](https://github.com/agiresearch/ASB)，[论文v3](https://arxiv.org/html/2410.02644v3) | 注入、记忆投毒、后门等；AIOS／ReAct模拟API；ASR主要看攻击指定工具，PNA看标签工具调用，RR用LLM | 记忆／多阶段风险备选；移植成本较高。与Agent-SafetyBench不同；检测FPR不是业务误拒，目标工具调用不等于实际泄露 |
| [InjecAgent](https://github.com/uiuc-kang-lab/InjecAgent)，[评分代码](https://github.com/uiuc-kang-lab/InjecAgent/blob/main/src/output_parsing.py) | 预设已读到污染工具返回，检查后续一至两步；主要按目标工具名判成功 | 轻量局部探针；不含完整任务成功环境，不核验所有参数或真实泄露。MIT，旧模拟模型替换需记录 |
| [AgentDAM](https://github.com/facebookresearch/ai-agent-privacy)，[论文](https://arxiv.org/html/2503.09780v1) | 正常任务中的隐私最小化；WebArena／VisualWebArena浏览器环境；终态utility＋关键词和上下文judge检查每步敏感披露 | 隐私专项，非注入集。网站部署／重置较重；许可含CC-BY-NC及部分模型衍生条件，今晚后延完整环境 |
| [ToolEmu](https://github.com/ryoungj/ToolEmu)，[论文](https://arxiv.org/html/2309.15817v1) | 模糊要求、信息不足与操作风险；LLM模拟工具／状态，安全和帮助由LLM评分 | 可补操作风险，不能提供确定性实际环境成功。每步模拟加多裁判成本较高，Apache-2.0，后延 |
| [BIPIA](https://github.com/microsoft/BIPIA)，[论文v3](https://arxiv.org/html/2312.14197v3) | EmailQA／WebQA／TableQA／摘要／代码材料注入；攻击类别采用字符串、语言或LLM判定 | 资料入口局部补充，无Agent工具循环。代码MIT，部分原数据需按许可另取；当前仓库已归档，固定来源 |
| [Tensor Trust](https://github.com/HumanCompatibleAI/tensor-trust-data)，[论文](https://arxiv.org/html/2311.01011v1) | 真人提示劫持／秘密抽取游戏；字符串判断access granted和秘密泄露 | 非Agent工具环境，真访问码对照不代表办公任务；数据许可未明确解决，暂不纳入运行，不能用论文许可代替数据许可 |
| [HarmBench](https://github.com/centerforaisafety/HarmBench)，[论文](https://arxiv.org/html/2402.04249v1) | 有害用户请求／越狱；行为分类器或专项规则评回答，非实际工具效果 | 入口／输出补充，非完整Agent任务。模型裁判权重与推理成本；攻击搜索非必需，可选公开样本，MIT代码另查权重 |
| [JailbreakBench](https://github.com/JailbreakBench/jailbreakbench)，[论文v3](https://arxiv.org/html/2404.01318v3) | harmful／同主题benign单轮对照，模型判断越狱成功和拒绝 | 可补输出与拒绝；没有业务终态环境。judge可能很重，原模型配置必须固定；与HarmBench／AdvBench来源重叠 |

Agent-SafetyBench整理过Dojo、InjecAgent和ToolEmu等来源，AgentDyn继承Dojo，JBB与HarmBench有重叠；混合前按基础任务和来源关系去重并保持同分区。clean utility下降不能直接记误拒，需结合明确拒绝／防护阻断事件；原judge失败记未知，不记安全。

## 推荐组合与顺序

1. **办公协作优先**：AgentDojo Workspace／Slack作为完整任务主干；加入SpreadsheetBench文件工作，保留各自环境和原评分。AgentHarm适配明确相关的直接恶意请求切片，Agent-SafetyBench补权限／参数等风险，按来源分层统计。
2. **软件工作台**：AgentDyn GitHub提供正常外部指令、仓库／文件操作及原生攻击判定；SWE-bench提供真正补丁与测试任务。GitHub模拟协作与真实代码修复分别保留环境，不能互称同一任务执行器。
3. **资料与文档**：AgentDyn DailyLife及Dojo中有依据的材料处理任务，搭配AssistantBench或获准GAIA；BIPIA只作材料注入专项。需要图像、文件生成时选有相应输入和评分的任务，不把图片固定附到每道问答。

这比三个场景都使用“问答后检测”更能覆盖实际读写、工具效果和不同交付。每个场景最终混合清单须同时有多类正常工作与多类风险；暂未覆盖的任务类别明确待补。

**今晚优先路径**是Dojo的现成状态工具与原判分，随后Dyn复用环境接口；SpreadsheetBench加入真正文件工作。先记录实际接入／耗时，再确定任务清单、训练量和重复数。不等待受门控账户，不先建设浏览器整站或桌面环境。主实验仍须具备所声明的真实防护池和方法；不能用较少库的开发批次冒称七至十来源完整比较。

## 接入和评价边界

换被测Agent为SeamPact是正常评测接口适配；原环境、业务工具、注入位置、完整轨迹和评分保持对应。改变这些语义时逐项记录。抽样与混合后的成绩以本研究来源子集报告，不混称官方完整榜单。

原生任务／攻击评分保留各自字段。实验记录器包围同一业务执行，训练可读取已结束任务的评分；业务模型和在线策略不能读取金标、未来到达、攻击身份或裁判答案。奖励转换另行冻结，论文直接给原指标和成本。
