# 从真实 API 候选到原生 SDK 验收

这一步直接消费已经完成的 API 实验记录和原草案 ZIP，不重填边界、不筛掉失败候选、不重新生成包。当前实现覆盖独立包接收检查和一个固定 CRAG 原生 SDK profile，完整生产准入仍未实现。

四源试跑使用同一观察契约和通用规则，共74轮。CRAG的7个边界、DiSP的9个边界分别生成完整BUILT_DRAFT；MMCT的19个边界中注册前H未通过，ABC的2个边界没有源码锚点，因此完整提交未构建。分析完成、包可构建、原生行为通过分别统计。

## 独立包接收

`seampact.evaluation.inspect_package`先核对外部实验记录提供的ZIP摘要，再检查精确文件清单、路径、重复项、symlink、每个工件hash、冻结源码清单和source_version。adapter和校验器必须是引擎拥有的代码；不会导入包中的未知代码。

随后重新运行源码核验，对每处R从原工件独立重放有限变换。recipe、patched工件以及Notebook其他单元、数据字段必须与独立重放完全一致。即使篡改者重算包内hash，改算法、替换adapter或伪造报告也会被拒绝。

`match_discovery_record`核对包、原候选、原报告和源码版本与保存的真实实验记录一致。这是实验来源匹配；不是对API调用的密码学证明，也不签发运行许可。

## CRAG固定profile

```text
PYTHONPATH=src python -B tools/eval_crag_package.py <原API草案.zip> <对应crag-text.json> <输出目录>
```

使用可选CRAG SDK环境。原Notebook必须匹配已冻结hash；保留原事件和全部step方法，显式提供真实Workflow／Context.store／Event／Node类型。仅OpenAI、Tavily、VectorStoreIndex、SummaryIndex四个外部端点采用受控实现。不会调用真实模型、搜索网络、下载数据或加载训练模型。

所有提交的边界必须由该profile映射和安装。不能只运行其中一个R，再把整包写为通过。当前实际API包含6个W和1个R，7个边界都在9个配对案例中被实际触发。

参考运行独立观察原步骤的进入次数和原LLM循环发生次数。适配运行核对：

- SDK真实登记集合、方法签名和step元数据保持对应。
- 原输出、异常、取消、端点顺序和Event复制语义一致。
- 每个候选边界的实际发生次数与参考运行一致；每次都等待许可、只启动一次、物理退休后槽归零。
- 9个案例覆盖直接步骤、完整查询、搜索分支、ingest、空输入、模型变化、异常与取消。

同一原生owner的槽由SDK真正触发的步骤共享，原SDK继续决定事件激活和分支。多个步骤同时使能时，验收器逐个发许可，不把未开始的调用当成已执行。

Windows的profile第1版通过，首次Linux复现暴露结束事件与授权快照的竞态，失败报告单独保留。第2版在再次授权前核对取消，并等待SDK清理对应门禁后再关闭账本。逻辑调用覆盖与原body进入分别记录：只有冻结原源码中在任何端点／状态效果前直接返回None的启动分支，允许被原SDK结束清理取消而未进入body；其他body进入次数必须一致。该例外在第2版观察契约中显式声明，报告保存每例差异，不能把它算成实际执行。

## 真实接线发现

该SDK使用`inspect.ismethod`登记步骤。把包装函数直接赋给实例会导致步骤未登记。通用方法桥接器保持原绑定方法的descriptor、签名和元数据，避免重复传self；同时避免显式未绑定签名被SDK的`functools.wraps`复制后引入虚假的self参数。

新增负对照刻意漏装一个准备步骤门禁。原结果和端点轨迹仍相同，但调用覆盖检查检测到了绕过。这说明答案一致不能替代接线覆盖和唯一启动检查。

报告`crag-api-package-result.json`保存原包hash、实验记录、案例、每个边界覆盖、负对照、SDK／Python版本和验收代码hash。包接收层本身不执行原流程；profile报告单列实际运行。页面将这项API草案证据与手选机制探针分开。

## Docker复现

把原ZIP和对应`crag-text.json`放入自己新建的只读输入目录。在`compose.env`添加其公开路径和两个文件名：

```text
SEAMPACT_API_INPUT_DIR='/home/<用户>/seampact-workspace/api-input'
SEAMPACT_API_PACKAGE='90e9790147a94747a0ffba8f1f9ffe7e.zip'
SEAMPACT_API_RECORD='crag-text.json'
```

然后执行独立项目的`--profile api-profile run --rm eval-crag-api`，使用已构建CRAG SDK镜像。运行网络none、不挂Key、结果写入`reports/crag-api/`，控制页面只读展示。具体Compose参数见[Docker指南](Docker部署.md)。

通过状态只适用于这个冻结包和受控profile。真实检索质量、外部效果闭合、数据／模型驻留、独立组合、并行和远程仍待各自验收；DiSP包只完成独立接收检查，训练与CLI行为尚未运行。
