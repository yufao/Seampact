# MADL 资源与原宿主说明

> 历史专项记录：下文状态和操作按原日期理解；当前实现见[当前实现](当前实现.md)，来源现状见[原生运行考察](../../ccfa-workfiles/experiments/harness-paper-design/native-feasibility.md)。

更新：2026-10-20。本文整理固定原库与已保存的公开资源事实，供用户在实验室大显存机器手动准备；不是分析器生成的入口、构造、参数映射或拆分结果。完整 pipeline 仍为 NOT_RUN；独立原 `DualStreamRuntime.predict` 已通过 API 生成的 Harness Unit 完成原 checkpoint 加载和一次真实图片预测。平台本轮分析提出 5 项权重需求，其中 DualStream 映射通过并已绑定，adapter／Qwen 基座／SAM／ranker 4 项仍 MISSING。组件成功不替代原完整初始化，也不构成鉴伪／定位质量结果。原始事实见 `var/prototype/research/original-intake/madl-resources-20261020.json`，原接入反馈见 `var/prototype/research/configuration-checks/resource-intake-results-20261020.json`，当前组件及冻结资源核对见[独立原组件报告](../var/prototype/research/configuration-checks/madl-native-component-result-20261020.md)。

## 固定来源与资源

原库为 [shannima/MADL](https://github.com/shannima/MADL/tree/af174679d88c7b605c56de34878e5ab2528f6794)，固定提交 `af174679d88c7b605c56de34878e5ab2528f6794`。MADL 输出 `real`／`synthetic`／`tampered` 三类标签，只在局部篡改判定时输出定位 mask；不承担 QACD 的回答风险评分语义。

| 资源 | 固定来源与已观测大小 | 当前边界 |
|---|---|---|
| MADL 配套包 | [moindy/MADL@529e9ddd19586c18f9b47685240adcff5fa87007](https://huggingface.co/moindy/MADL/tree/529e9ddd19586c18f9b47685240adcff5fa87007)，仓库总计 613,351,673 字节；含 LoRA、dual-stream、visual ranker 及配套文件 | 本机已下载到 `var/prototype/models/MADL-529e9ddd`；MANIFEST 的 8 项模型／配套工件合计 613,334,218 字节，大小与 SHA256 逐项一致；DualStream 已原生加载／预测一次，其余模型与完整 pipeline NOT_RUN |
| Qwen 基座 | [Qwen/Qwen2.5-VL-7B-Instruct@cc594898137f460bfe9f0759e9844b3ce807cfb5](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct/tree/cc594898137f460bfe9f0759e9844b3ce807cfb5)，仓库 16,595,981,281 字节，其中 5 个权重分片合计 16,584,414,560 字节 | 本任务未下载或加载；此 SHA 是资源盘点版本，原 adapter 的 `revision=null`，原 loader 不自动采用本页固定版本 |
| SAM ViT-H | [官方 checkpoint](https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth)，HEAD 返回 200、Content-Length 2,564,550,879 字节 | 不在 MADL 配套包内；尚未下载 payload 或计算 SHA256，更未加载 |

配套包的[固定 MANIFEST](https://huggingface.co/moindy/MADL/blob/529e9ddd19586c18f9b47685240adcff5fa87007/MANIFEST.json)给出 8 个发布文件的大小和 SHA256；它不包含 SAM，也不包含 Qwen 基座。取得约 613 MB 配套包不等于完整模型已齐备。

本机完整性结果见 `var/prototype/research/original-intake/madl-download-result-20261020.json`。下载还包含 README 等非模型文件；上述 8 项是 MANIFEST 工件分母，不把全部文件数当成模型完成比例。平台已分别登记 adapter、dual-stream、visual ranker 三项权重并检查文件存在；只有 DualStream 已经实际反序列化、执行原 strict 校验和预测，不能扩展为其他权重已加载。Qwen 基座与 SAM 仍未下载。

## 已运行的独立原组件

任务 `81c16829c6c74809aa7f6e9667cbe753` 只调用原 API 候选导出的 `9524fbc58b6444428d3df357f66ef134:agent_b_dualstream_pixel` 一次。API 候选和零模型绑定候选 canonical SHA256 相同；66 个冻结原文件及 11 份分析实现归档、真实 checkpoint SHA256、Store 任务与冻结资源身份均已核对。该入口保留原 `DualStreamRuntime` 构造与进程内 owner，不代替完整 `build_pipeline`。

原绑定／构造为 10,832.100 ms，predict 边界为 887.774 ms，对话端到端为 20,553.300 ms；4 个对话响应共 13,043 tokens，范围不同不相加。输入是真实公开 StoryBench JPG，无 MADL 类别或 mask GT，质量 NOT_EVALUATED；原 ndarray 热图只留本地 owner，未保存像素。费用、显存峰值和纯 GPU 时间 UNKNOWN。

本次组件依赖与现有 Pyserini／pdfplumber 工作负载存在已记录的版本范围冲突；精确环境清单及安装记录保留在私有运行工件中。组件成功不证明其他工作负载兼容。

## 原宿主、显存与路径语义

原 `src/madl/factory.py` 在返回 pipeline 之前，构造 semantic runtime、dual-stream、SAM segmenter 和 visual ranker。它们属于原完整初始化，不能因为后来只激活某个拆分体就省略。原 `src/madl/backbones/qwen.py` 从 adapter 配置读取 Qwen 基座，按 BF16 加载后执行 `.to("cuda")`；原 OOM 分支仍使用 CUDA。CLI 的 `--device cpu` 不会解除 Agent A 的这项原始要求。

完整初始化需同时容纳 Qwen BF16 权重、SAM、其他网络、激活和推理开销。本地尚未完成原全量 CUDA 加载，未改量化、offload、基座或模型加载方式。应在具备足够显存的原生 CUDA 宿主运行，具体资源余量、驱动、依赖和加载结果保存在本地部署记录。

在已检查的 `src/**/*.py` 和包入口中未发现原 HTTP 模型客户端／服务入口。单独提供 Qwen HTTP URL 不能交给这些原模型对象接口；本范围不编写 MADL HTTP 包装。要在大机器运行现有 MADL Unit，平台及原本地执行宿主也应位于能直接读取权重、使用 GPU 的环境；这不是通用 Unit 跨机器迁移功能。

`MADLConfig.weight_root` 默认是 `models`，`resolve_weight` 实际计算 `Path(weight_root) / Path(relative)`。相对路径以**原执行进程 cwd**为基准，不以源码 `__file__` 或 YAML 文件父目录重定位。原 CLI 接受 `--weights`，默认取 `MADL_WEIGHT_ROOT` 或 `models`，并用该值覆盖配置中的根路径。因此它是**原本可配置的权重根路径**，不是必须放在源码 checkout 下的固定位置。平台最终宿主／绑定尚未确定时，不把 checkout 示例路径标为实际运行位置；实际位置应以平台依赖配置中的原 cwd、解析结果和缺失文件为准。

**VPC3 资源静态绑定续接（早先记录）：** 通用原构造路径核对现覆盖已验证的 `Path` 规范化、字面相对子路径、固定一层解包与 `joblib.load` 导入来源；未修改原库或 API 候选。4 份真实候选重检保留 MADL／REX 原结果，VPC3 gate 需求转为 MAPPING_CHECKED；可选 Qwen3 服务仍因实际目标字段缺资源引用为 MISSING。选择已登记 gate 资源后形成零模型绑定 `c4ebff17798247a09b390b3d387592c4`，候选保持不变，`vpc3_calibrate` READY，初始化仍 NOT_RUN。gate 反序列化与 calibrate 均未运行：实验室服务未部署，亦无真实 expert evidence，不构造虚假请求。详见 [vpc3-constructor-path-check-20261020.json](../var/prototype/research/configuration-checks/vpc3-constructor-path-check-20261020.json)、[vpc3-gate-binding-20261020.json](../var/prototype/research/configuration-checks/vpc3-gate-binding-20261020.json)。

**零模型绑定的实现与成本归档：** `analysis.py.freeze_implementation` 现在归档进程启动时冻结的 12 份实现文件（新增 `service.py`）；`bind_dependency` 也保存当时实现摘要／归档及 `binding_duration_ms`。重启后使用同一 VPC3 gate 与未改变的 API 候选再次选择资源，该次绑定为 `fce3ef480fc9402585b9ec4a04f62dae`，0 模型请求，12 份归档摘要与当时源码全部匹配；绑定 823.3083 ms、HTTP 请求端到端 845.5794 ms 分列，不相加。原 `calibrate` 仍 READY／初始化 NOT_RUN，没有原模型加载或目标调用。旧 `c4ebff17798247a09b390b3d387592c4` 记录原样保留，不倒填当时未记录的实现归档。证据见 [zero-model-binding-archive-20261020.json](../var/prototype/research/configuration-checks/zero-model-binding-archive-20261020.json)。

**VPC3 当前资源与绑定：** 原 Windows checkout 的 `gate_model.json`／`artifact_manifest.json` 被检出为 CRLF，与原 manifest 的 Git blob 字节摘要不同；旧资源 `34aa960a720a20dad19a0c371236c2f6` 补入原 SHA256 与大小后，真实检查为 `checksum_or_size_mismatch`，不能继续以“文件存在”表示资源可用。没有修改 checkout、JSON 值、原 manifest 或原候选；从固定 commit `c114215f31d03d37b07376558203babd984692b1` 逐字节提取三份原 Git blob 到 `var/prototype/models/VPC3-gate-c114215f-canonical`，`ACQUISITION.json` 记录提取依据及 checkout 前后不变。新资源 `ddd0c1eae213bbe1ae39a22823a60c6d` 的三份原文件 SHA／大小及原 spec／manifest 全部匹配。

通过未改动的原 API 映射选择新资源后，当前绑定为 `d55a675445544181a91ebb4b0205be58`，候选与前次 `fce3` 完全相同、0 模型请求、12 份实现归档与当时源码一致，READY／初始化 NOT_RUN。绑定 832.3704 ms、HTTP 端到端 855.8683 ms 是嵌套范围，独立于前次 `fce3` 成本保留。`c4eb`／`fce3` 的旧资源与实现证据原样保留。`bind_dependency` 状态现在先 RUNNING 再 COMPLETED，安装异常记 FAILED 并在 finally 记录时长；仅成功真实路径已核对，未构造失败实验。原 gate 反序列化、calibrate 和专家调用仍 NOT_RUN，Qwen 服务真实引用链仍 MISSING／未部署。证据见 [vpc3-canonical-resource-20261020.json](../var/prototype/research/configuration-checks/vpc3-canonical-resource-20261020.json)、[vpc3-canonical-binding-20261020.json](../var/prototype/research/configuration-checks/vpc3-canonical-binding-20261020.json)。

**PaperGraph 与当前研究草稿：** 原服务流只读审查记录了 judge／target／embedding／vision／OCR 5 条跨配置链，以及候选中 33 处缺对应 mapping 的资源引用；当前为 0 绑定，完整在线目标 NOT_RUN。helper／dataclass／HTTP 字段等建议规则均 NOT_IMPLEMENTED，审查不能替代分析器生成映射。当前研究表单草稿已从旧人工包装 Unit 换为成功命名 REX 组的原场景及 UTF-8 任务 ID，程序标准／独立评价标准和教师全文完全保留，未启动新 run。材料见 [papergraph-resource-flow-20261020.md](../var/prototype/research/configuration-checks/papergraph-resource-flow-20261020.md)、[research-current-draft-20261020.json](../var/prototype/research/configuration-checks/research-current-draft-20261020.json)。Linux 实机及跨机器服务实际调用仍为 NOT_RUN。

## 在实验室手动准备

下面 `/srv/seampact/models/MADL-529e9ddd` 仅为用户可选择的绝对权重根目录示例，不是平台已经绑定的位置。将固定配套包放入该根目录，另放 SAM：

```text
/srv/seampact/models/MADL-529e9ddd/
├── agent_a_qwen_lora/
│   ├── adapter_config.json
│   ├── adapter_model.safetensors
│   ├── chat_template.jinja
│   ├── processor_config.json
│   ├── tokenizer.json
│   └── tokenizer_config.json
├── agent_b_dualstream/madl_agent_b_dualstream_v1.pt
├── agent_b_visual_ranker/madl_agent_b_visual_ranker_v1.pt
└── external/sam_vit_h_4b8939.pth
```

已有可用 HF CLI 时，固定版本获取命令为：

```bash
hf download moindy/MADL \
  --revision 529e9ddd19586c18f9b47685240adcff5fa87007 \
  --local-dir /srv/seampact/models/MADL-529e9ddd

hf download Qwen/Qwen2.5-VL-7B-Instruct \
  --revision cc594898137f460bfe9f0759e9844b3ce807cfb5 \
  --cache-dir /srv/seampact/hf-cache
```

SAM 按上面的官方 checkpoint 链接另行下载到示例根目录的 `external/sam_vit_h_4b8939.pth`。配套文件与 MANIFEST 逐项核对；SAM 没有已保存的完整 payload 摘要，不能以 HEAD 的 ETag 冒充 SHA256。

Qwen 原引用来自 adapter 配置的 `base_model_name_or_path=Qwen/Qwen2.5-VL-7B-Instruct`，没有 revision。单独下载一个本地目录不会重定向该引用；固定 revision 下载到某个 HF cache 也不证明原 loader 最终选择该 revision。需让原执行账户／进程能访问实际使用的缓存，并记录实际解析的 snapshot。若平台尚不能在不改原加载语义的条件下固定或核对该版本，保留版本 UNKNOWN／对应缺口，不擅改 adapter 配置或原源码来伪装已经绑定。

原库 [README](https://github.com/shannima/MADL/blob/af174679d88c7b605c56de34878e5ab2528f6794/README.md)建议 Python 3.10 与 NVIDIA CUDA；推理依赖在同一提交的 `pyproject.toml` 和 `requirements.txt`。本页不指定未经实机核对的新版本组合。准备好原包环境后，原 CLI 为：

```bash
madl-infer \
  --image /path/to/actual-image.png \
  --weights /srv/seampact/models/MADL-529e9ddd \
  --output-dir /srv/seampact/outputs/example \
  --device cuda
```

该命令保留原 `madl.cli:main → build_pipeline → predict`，会输出 `<stem>_madl.json`，必要时输出 `<stem>_madl_mask.png`；不是本文已执行的命令，也不是平台的手写入口或映射。用户准备后提供宿主 GPU／环境版本、实际资源根目录、Qwen 实际 snapshot 与原 CLI 加载／推理日志即可继续核对；密钥仍只在平台私有凭据入口填写。平台随后仍需让分析器补齐原源码证据、通过映射及构造检查，再由生成的 Harness Unit 调用。独立三类标签和 mask 评价材料也需另外准备。
