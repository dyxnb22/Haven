# 模块 05——有界代理循环与上下文工程

> 文件：`src/haven/application/run_actor.py`、`src/haven/application/answer_resolution.py`、
> `src/haven/application/run_service.py`（装配与委托）、`src/haven/application/context_builder.py`、
> `src/haven/domain/budget.py`、`src/haven/domain/stuck.py`、`src/haven/domain/transitions.py`
> 测试：`tests/integration/test_agent_journeys.py`、`tests/integration/test_provider_retry.py`、
> `tests/unit/test_context_builder.py`、`tests/unit/test_budget.py`
> ADR：[0006——长程规划与预算](../docs/adr/0006-long-horizon-planning-and-budgets.md)

## 学习目标

- 写出一个有限循环，让每次运行都以且只以一个停止原因结束；
- 强制执行代理无法提高的硬预算，并识别卡死循环；
- 让上下文由程序确定性选择，而不是无限累积；
- 安全重试模型调用，并明确什么时候绝不能重试。

## 本课路线

- **前置：** 模块 01 的 State / Context，模块 03 的 `ToolResult`。
- **建议用时：** 60–75 分钟。
- **阅读顺序：** 先从 `StopReason` 枚举和预算测试建立边界，再读 `_drive` 主循环，最后单独研究
  `ContextBuilder` 和 provider retry；不要同时追四条支线。
- **最小验证：**

```bash
uv run pytest -q tests/unit/test_budget.py tests/unit/test_stuck.py \
  tests/unit/test_context_builder.py tests/integration/test_provider_retry.py
```

## 循环的职责

`RunActor.drive` 可以概括为：

```text
Model → Tool(s) → Observation → Model → …
```

它持续运行，直到程序决定停止。每一步开始前检查预算、计入步数、构建 Context，然后流式调用模型。
模型如果提出工具，就交给工具通道；如果给出最终回答，就交给 Evidence Gate 判断。

`RunService._drive` 是这之外的装配层：冻结交接笔记、准备运行独占的临时目录、创建 Actor
并注册，然后委托。把循环放进 Actor，是为了让一次运行的转录、用量和证据只有一个归属，
暂停、恢复和取消因此都有明确的所有者。

系统只有一个 `_finish`，而且它总会写出一个 `StopReason`。例如
`final_answer`、`evidence_satisfied`、`evidence_missing`、`step_budget_exhausted`、
`no_progress`、`provider_error`、`cancelled` 和 `verification_unavailable`。

运行不能在未知状态里结束。状态迁移经过 `domain/transitions.py`；非法迁移直接抛异常。

## 预算是代理无法抬高的天花板

`domain/budget.py` 管理步数、工具调用次数、墙上时间、token 和成本。`.haven.toml` 只能进一步
收紧这些限制，循环里的任何内容都不能把它们调大。

ADR 0006 不只记录默认值（24 步、48 次工具调用），还记录推导过程：最小成功轨迹需要
read/edit/create/diff/check/answer，再加大约三轮修复—验证。第一版预算是 12/24，刚好在本应恢复时
截断运行，只留下一个 `step_budget_exhausted`，掩盖了真正结果。

预算应根据真实工作量设置，并在评估中验证；命中预算时必须产生明确的停止原因。

步数或工具调用耗尽时，循环在停止之前还给一次**剥离工具**的收尾调用：模型已经
不能再动手，但往往已经读过关键文件，直接停会把"步数用完了"当作全部交付物。这次
调用不计步骤、不计工具调用，token 照常计费并出现在报告里——它确实花了钱，瞒报比
花钱更糟。墙上时间、token 或成本耗尽时**不**给这次调用：那些正是"别再花"的信号，
再调一次模型等于违背它。

## 卡死循环要尽早停

`domain/stuck.py` 记录工具、参数和结果。如果三次连续调用完全相同，运行判定为 `no_progress`。
检测不依赖剩余预算，因此能在耗尽 token 前停止重复行为。

## Context 是选择结果，不是聊天记录的堆积

打开 `context_builder.py`，先看布局：

```text
稳定头部：系统规则 · 技能目录 · AGENTS.md 指导 · 选中的技能正文 · Task: {goal}
           transcript（追加记录）
易变尾部：task plan · run status（步数/工具计数器）
```

技能目录（有哪些技能、各自的 digest）常驻头部，因为正是它让渐进披露成立；
技能正文只有用户显式选择后才进入。两者都是不可信数据——技能是提示片段，
不是工具，它提到的脚本不会因此变成受信工具（ADR 0031）。

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

## 委派与双轨：把上下文和时间花在刀刃上

循环内还有两个“看起来是模型决策、实际是程序决策”的分支：

- **快慢双轨。** `_choose_model` 在单模型配置下直接返回；配了 `ModelRouter` 时，由它按
  是否已发生编辑、计划是否开始实施、快轨轮数是否用尽来选模型。模型选择发生在**构建请求
  之前**，并且贯穿重试（ADR 0042、0049）。
- **只读子代理委派。** `agent.delegate` 起一个独立的子模型循环，父运行**同步等待**。
  子运行的工具集被 `CHILD_READ_TOOLS` 白名单锁死（列表、搜索、读取、大纲、符号、diff），
  预算钳制到父运行剩余并预留一轮，退出时统一回记账。它省的是父转录的空间，不是墙上的时间
  （ADR 0050）。

## 计划模式：先想清楚，再动文件

`/plan GOAL` 启动一轮**只读**的规划运行：工具集收窄到只读加计划写入，工作区不可写。
规划可以多轮修订，但任何一轮都不能改文件。`/implement`（或 `haven implement RUN_ID`）
是独立的用户入口，它把已就绪的计划确认成一次正常的执行运行——审批、沙箱和证据门禁
一概不变。

这个拆分解决的是一个具体问题：让同一套循环既能“只商量不改”，又能“改”，靠的是模式
切换时收窄工具集，而不是靠提示词请模型自律。计划步骤有稳定 ID、依赖、阻塞原因和验收
标准，因此“做到哪一步了”是程序可判定的，不是叙述。

## 延伸阅读

- ADR 0006（规划与预算）和 ADR 0008（前缀排序）。
- 提交 `958d98a`（循环）、`9d8e684`（`perf(context)`）、`a52b886`（重试）。
