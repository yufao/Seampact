# VPC3 Qwen3-VL 专家服务部署

2026-10-20：以下提供专家服务的资源与接口配置；服务启动和原 VPC3 推理尚为 NOT_RUN。实际机器、下载进度及部署交接记录保存在私有运行目录。

模型固定为 `Qwen/Qwen3-VL-8B-Instruct`，revision 为 `0c351dd01ed87e9c1b53cbc748cba10e6187ff3b`。原 BF16 权重共 17,534,339,512 字节，还需视觉计算、激活和 KV cache 的显存。使用原模型的图像 Chat Completions 服务，保持原门控、原专家提示词及请求流程。

下面是 Linux／NVIDIA 机器的独立环境示例；没有在实验室执行。vLLM 官方 Qwen3-VL 指南要求至少 0.11.0，本例固定该版本，并固定 tokenizer／processor 使用的 Transformers 版本。具体 GPU、驱动、实际安装结果和启动参数随部署记录。[官方指南](https://github.com/vllm-project/recipes/blob/main/Qwen/Qwen3-VL.md)、[0.11.0 支持模型](https://docs.vllm.ai/en/v0.11.0/models/supported_models.html)。

```bash
python3.11 -m venv .venv-qwen3-vl
source .venv-qwen3-vl/bin/activate
python -m pip install 'vllm==0.11.0' 'transformers==4.57.6' 'huggingface_hub==0.36.2'

hf download Qwen/Qwen3-VL-8B-Instruct \
  --revision 0c351dd01ed87e9c1b53cbc748cba10e6187ff3b \
  --local-dir ./models/Qwen3-VL-8B-Instruct

vllm serve ./models/Qwen3-VL-8B-Instruct \
  --served-model-name Qwen3-VL-8B-Instruct \
  --dtype bfloat16 \
  --host 0.0.0.0 --port 8001 \
  --max-model-len 8192 --max-num-seqs 1 \
  --limit-mm-per-prompt '{"image":1,"video":0}'
```

此配置限定单图、单并发和 8192 token 上下文，适用于当前原 VPC3 单图请求；显存是否足够仍以实际启动和图像请求为准。上下文不足应记录失败并调整资源配置，不静默截断图像或换量化权重。多卡部署时显式记录 `--tensor-parallel-size`。参数含义见 [vLLM 固定版 CLI](https://docs.vllm.ai/en/v0.11.0/cli/serve.html)，权重与原架构见 [固定模型卡](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct/blob/0c351dd01ed87e9c1b53cbc748cba10e6187ff3b/README.md)。

启动后提供以下信息即可继续平台绑定：

| 字段 | 值／用途 |
|---|---|
| Base URL | `http://实验室机器地址:8001/v1`，需能从平台进程访问 |
| Served model name | `Qwen3-VL-8B-Instruct`，与原类默认名称一致 |
| Model revision | 上述固定 SHA，不能只填写模型别名作为版本依据 |
| 运行记录 | 实际 GPU、启动命令、vLLM／Transformers／torch 版本、启动日志；`python -m pip freeze` 可保存环境列表 |

原 `VPC3Calibrator` 通过 `qwen3_base_url`、`qwen3_model_name`、`qwen3_model_revision` 取得专家服务；已有 `gate_dir` 保持原值。地址准备后，通过平台现有显式资源绑定更新原入口构造参数，形成新分析／安装身份，再使用生成的 Unit 开展真实图像调用和对照，不写来源包装。

原客户端发送实际图像 base64 与原提示词，等待非流式 JSON `choices[0].message.content`；该服务需支持这一返回形式。VPC3 当前使用占位 `EMPTY` 作为本地兼容服务凭据；若实验室端点要求真实鉴权，应先明确接口接线，不能把密钥填进普通来源配置。

本机已装 Transformers 4.57.6 的 `transformers serve` 路由总是返回 SSE，不能直接冒充原客户端可用的专家服务；只读源码依据见 `qwen-official-serving-inspection-20261020.json`。这也是此处选择官方 vLLM 兼容服务的原因。服务启动、原 VPC3 校准完成及调度收益分别记录；尚无实验室推理结果。
