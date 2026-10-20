# 构建生产级编码代理：Haven 课程

[English](archive/en/README.md) | **中文**

这套课程从 Haven 的真实代码出发，讲清一条编码代理如何从“模型会调用工具”走向安全、可验证、可恢复。
你不需要 Agent 经验或 API key；预备课会补齐必要概念，示例与测试默认离线运行。

贯穿课程的原则是：

> 模型负责提案；确定性的程序负责权限、执行和成功判定。

英文原稿保存在 [`archive/en/`](archive/en/)。学习时以当前源码、测试和中文课程为准；ADR 和历史文档
用于解释设计演进，不自动代表当前行为。

## 你会学到什么

学完后，你应能从 Haven 的代码和 Trace 解释：

- 工具调用为什么只是提案，以及唯一执行通道如何强制这条边界；
- State、Context、ModelResult 与 Trace 的所有者和生命周期；
- 有界循环如何处理预算、卡死、上下文和停止原因；
- 审批如何绑定精确动作，防止参数漂移、重复消费与 TOCTOU；
- 为什么最终文本不等于成功，编辑任务需要哪些客观证据；
- 副作用中断后，系统如何避免重复执行；
- 离线评估、在线评估和成本指标各自能证明什么；
- 如何用收益门禁决定构建、推迟或放弃一个功能。

## 开始前

你只需会读基本 Python 函数、类和 JSON。`async`、Protocol、Pydantic 与 pytest 可在预备课 B 现学。

先读 [`docs/LEARNING.md`](../docs/LEARNING.md)，再运行：

```bash
uv sync --locked
uv run haven doctor
uv run pytest -q tests/unit/test_policy.py
```

`doctor` 不会调用付费模型。每个阶段结束后再运行全量验证：

```bash
uv run pytest -q
uv run haven eval --offline
```

## 学习方法

每课按三步完成：

1. **预测：** 读目标和主流程，先写下系统应怎样工作。
2. **追踪：** 从课程指定的测试进入源码，只跟一条数据流。
3. **验证：** 运行最小命令，再合上源码复述关键路径。

每课留下四行记录即可：

```text
本课不变量：
一条成功路径：
一条失败路径：
仍不能解释的问题：
```

用[源码伴读索引](code-reading-map.md)定位代码；能指出对应符号、测试或错误码，才算掌握。

### 代码优先约定

每个知识点都要落回四项项目证据：

1. 当前源码中的入口符号；
2. 固定行为的测试；
3. Trace 或结构化结果；
4. 你实际运行的验证命令。

若课程文字无法回答“在 Haven 的哪个符号、哪个测试里能看到”，先不要把它当作项目知识。

## 学习路线

零基础按顺序学习：

```text
预备课 A–C → 模块 00 → 模块 01–10 → 结业项目
```

- **预备课 A–C：** 认识消息、工具调用和有限循环；补齐源码阅读所需的 Python；跑通第一条离线 Trace。
- **模块 00：** 从十一个失败假设推导 Haven 的主要机制，建立整体因果链。
- **模块 01–10：** 按层深入契约、工具、安全、运行、恢复、界面、评估和工程判断。
- **结业项目：** 在现有边界内完成一次可运行、可验证的扩展。

如果时间有限，可走：`A → C → 00 → 01 → 03 → 04 → 05 → 06 → 07 → 09 → 结业项目`。
不要跳过 06、07、09：它们分别回答“何时算完成”“如何安全恢复”和“如何证明系统有效”。

| 阶段 | 建议用时 | 阶段产出 |
|---|---:|---|
| 预备课 A–C | 3–4 小时 | 能解释一条完整离线 Trace |
| 模块 00 | 90–120 分钟 | 失败假设 → 工程边界的因果链 |
| 模块 01–04 | 每课 45–60 分钟 | 信任边界图与精确审批解释 |
| 模块 05–06 | 每课约 60 分钟 | 停止原因表与成功证据清单 |
| 模块 07–08 | 每课 45–60 分钟 | 恢复三态与事件到屏幕的追踪 |
| 模块 09–10 | 每课约 60 分钟 | 指标边界与收益门禁 |
| 结业项目 | 1–2 天 | 改动、测试和决策记录 |

### 从哪门预备课开始

- 说不清 tool call 由谁校验、授权和执行：从[预备课 A](prerequisite-01-agent-basics.md)开始。
- 说不清 Protocol、Pydantic、`AsyncIterator` 和 pytest fixture 的职责：读[预备课 B](prerequisite-02-python-reading-kit.md)。
- 不会从 Trace 区分提案、授权、执行与成功：读[预备课 C](prerequisite-03-first-offline-journey.md)。

## 课程地图

| 编号 | 课程 | 核心产出 |
|---|---|---|
| A | [从一次模型调用到 Agent](prerequisite-01-agent-basics.md) | Chat、Workflow、Tool Calling 与 Agent 的边界 |
| B | [读懂 Haven 所需的 Python 与测试](prerequisite-02-python-reading-kit.md) | 类型、端口、异步、依赖注入与测试分层 |
| C | [第一次离线运行与 Trace 导览](prerequisite-03-first-offline-journey.md) | 六轮修复旅程与事件簇阅读法 |
| 00 | [从零构建可靠 Agent](00-from-scratch.md) | 十一个失败假设及其修复链 |
| 01 | [心智模型](01-mental-models.md) | State / Context / ModelResult / Trace 分类 |
| 02 | [Provider 契约](02-provider-contract.md) | SSE 到 `ModelEvent` 的转换链 |
| 03 | [唯一执行通道](03-tools-and-the-execution-channel.md) | 提案到 `ToolResult` 的门禁图 |
| 04 | [策略、审批与隔离](04-policy-approval-security.md) | 不可复用、不可漂移的审批 |
| 05 | [循环与上下文](05-agent-loop-and-context.md) | 停止原因、预算和重试边界 |
| 06 | [证据门禁](06-evidence-gate.md) | 编辑成功所需的最小证据集 |
| 07 | [持久化执行](07-durable-execution.md) | `not_run / confirmed / unknown` 恢复表 |
| 08 | [纯终端 UI](08-tui-pure-interface.md) | 事件 → reducer → 屏幕 |
| 09 | [评估与成本](09-evaluation-and-cost.md) | 机制、能力、安全与成本的指标边界 |
| 10 | [工程判断](10-engineering-judgment.md) | 一页收益门禁或 ADR |
| — | [结业项目](capstone.md) | 综合实践、测试和判断记录 |

卡住时先读测试名，再用 `rg "符号名" src tests` 找入口；文档与实现冲突时，以当前源码、测试和生成门禁
为准。第一次学习不要跳读，也不要从最大的 `RunService` 文件开始逐行阅读。
