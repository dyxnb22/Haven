# 模块 05——有界代理循环与上下文工程

[English](archive/en/05-agent-loop-and-context.md) | **中文**

> 文件：`src/haven/application/run_service.py`、`src/haven/application/context_builder.py`、
> `src/haven/domain/budget.py`、`src/haven/domain/stuck.py`、`src/haven/domain/transitions.py`
> 测试：`tests/integration/test_agent_journeys.py`、`tests/integration/test_provider_retry.py`、
> `tests/unit/test_context_builder.py`、`tests/unit/test_budget.py`
> ADR：[0006——长程规划与预算](../docs/adr/0006-long-horizon-planning-and-budgets.md)

## 学习目标

- 写出一个有限循环，让每次运行都以且只以一个停止原因结束；
- 强制执行代理无法提高的硬预算，并识别卡死循环；
- 让上下文由程序确定性选择，而不是无限累积；
- 安全重试模型调用，并明确什么时候绝不能重试。

## 循环的职责

`RunService._drive` 可以概括为：

```text
Model → Tool(s) → Observation → Model → …
```

它持续运行，直到程序决定停止。每一步开始前检查预算、计入步数、构建 Context，然后流式调用模型。
模型如果提出工具，就交给工具通道；如果给出最终回答，就交给 Evidence Gate 判断。

最值得复制的纪律是：**只有一个 `_finish`，而且它总会写出一个 `StopReason`。** 例如
`final_answer`、`evidence_satisfied`、`evidence_missing`、`step_budget_exhausted`、
`no_progress`、`provider_error`、`cancelled` 和 `verification_unavailable`。

一次运行不能在“好像停了”的未知状态里结束。如果你说不出它为什么停止，那不是日志问题，而是 bug。
状态迁移还会经过 `domain/transitions.py`；非法迁移直接抛异常，让状态机错误尽早暴露。

## 预算是代理无法抬高的天花板

`domain/budget.py` 管理步数、工具调用次数、墙上时间、token 和成本。`.haven.toml` 只能进一步
收紧这些限制，循环里的任何内容都不能把它们调大。

ADR 0006 不只记录默认值（24 步、48 次工具调用），还记录推导过程：最小成功轨迹需要
read/edit/create/diff/check/answer，再加大约三轮修复—验证。第一版预算是 12/24，刚好在本应恢复时
截断运行，只留下一个 `step_budget_exhausted`，掩盖了真正结果。

预算不是“让程序别跑太久”的模糊开关，而是一个有名字、有解释的运行结果。它应该根据真实工作量设置，
并在评估中验证。

## 卡死循环要尽早停

`domain/stuck.py` 记录工具、参数和结果。如果三次连续调用完全相同，运行判定为 `no_progress`。
这种检测便宜、确定，而且不依赖剩余预算；模型原地打转时，不会先把 token 全部烧光才被发现。

## Context 是选择结果，不是聊天记录的堆积

打开 `context_builder.py`，先看布局：

```text
稳定头部：系统规则 · AGENTS.md 指导 · Task: {goal}
           transcript（追加记录）
易变尾部：task plan · run status（步数/工具计数器）
```

这里有三个关键设计：

1. **信任标注。** 每个片段都带来源和 `trusted`/`untrusted` 标记，并写入 `context.built`
   Trace，也能通过 `haven debug-context` 查看。仓库中的 `AGENTS.md` 是不可信的 user message，
   永远不能进入 system prompt。早期实现曾把它混进 system role 并标成 trusted；那是一个真实 bug。
2. **确定性截断。** transcript 太大时，用 stub 替换最老的工具输出，而不是让模型写摘要。模型摘要
   可能把猜测写成事实；计划属于 State，每轮都会重新渲染，所以截断不会把它丢掉。
3. **对 prompt cache 友好的顺序。** 计划和预算计数器每轮都会变化，因此放在尾部；前面的字节保持
   稳定，provider 才能复用前缀缓存。模块 09 会讨论 71%→89% 的测量以及它的边界。

`tests/unit/test_context_builder.py::TestPrefixStability` 直接测试第三点：构建第 N 和第 N+1 轮，
确认新增 transcript 之前的前缀逐字节相同。

## 重试模型，但不要重试副作用

模型调用本身没有副作用，所以连接失败可以安全重试；工具调用可能已经写入文件，绝不能因为调用方没
收到响应就盲重试。

在线运行还发现，中途断流也可以重试：本轮组装中的文本和工具调用只存在于当前尝试，整轮完成前不会
进入 transcript。UI 收到 `stream.restarted`，丢弃已经显示的半截内容。重试逻辑和上限由
`tests/integration/test_provider_retry.py` 固定。

## 练习

1. **预测停止原因。** 运行 `test_agent_journeys.py` 中三个脚本流程，先预测各自的 `StopReason`，
   再查看结果。
2. **强制预算停止。** 使用 `Budget(max_steps=2)` 和一个永不结束的模型，断言它在第 2 步停止，原因
   是 `step_budget_exhausted`。
3. **证明前缀稳定。** 在两轮之间推进预算计数器，断言只有尾部消息变化。
4. **解释重试边界。** 为什么模型调用可以重试，工具调用不行？这依赖模块 03 的哪一条不变量？

## 自测

- 给出三个不同的 `StopReason`，以及各自的触发条件。
- 为什么不能用模型写的摘要做截断策略？
- 计划存在哪里？它为什么同时影响截断安全性和缓存命中率？

## 延伸阅读

- ADR 0006（规划与预算）和 ADR 0008（前缀排序）。
- 提交 `958d98a`（循环）、`9d8e684`（`perf(context)`）、`a52b886`（重试）。
