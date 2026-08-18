# 模块 03——工具与唯一执行通道

[English](archive/en/03-tools-and-the-execution-channel.md) | **中文**

> 文件：`src/haven/application/tool_pipeline.py`、`src/haven/application/registry.py`、
> `src/haven/contracts/tools.py`、`src/haven/domain/ticket.py`、`src/haven/domain/policy.py`
> 测试：`tests/integration/test_agent_journeys.py`、`tests/integration/test_tool_error_containment.py`
> ADR：[0002——工具执行边界](../docs/adr/0002-tool-execution-boundary.md)

## 学习目标

- 建立一条所有模型提案都必须经过的管线；
- 能指出每个门检查什么、拒绝什么；
- 解释为什么 executor 只能接收程序签发的 **ticket**，不能接收模型原始 JSON；
- 让工具失败变成结构化结果，而不是抛进代理循环的异常。

## 唯一通道

从模型提案到副作用，只有一条路：

```text
ModelResult
  → Tool Registry        （工具存在，版本固定）
  → Schema Validation     （严格 Pydantic 参数 → 稳定错误码）
  → Workspace Facts       （规范化路径、preimage digest、越界/保护检查）
  → Deterministic Policy  （allow / ask / deny；纯函数）
  → Exact Approval        （需要询问时；绑定 digest，一次性）
  → ExecutionTicket       （原始模型 JSON 在这里止步）
  → Executor              （原子工作区操作，或固定 argv 经过唯一 sandbox 包装点）
  → ToolResult + Evidence + Trace
  → 下一轮 Context
```

从上到下阅读 `ToolPipeline.execute`。每一个箭头都是一个有名字的安全步骤；这不是普通的编排函数，
而是安全模型正在运行的地方。

## 为什么需要 ExecutionTicket

`workspace_fs.py` 和 `process_executor.py` 中的 executor 永远看不到模型 JSON。管线会签发一个
`ExecutionTicket`，将工具名和版本、规范化参数、工作区身份、preimage digest 绑定成一个程序生成的值；
executor 只消费这个值。

这样“模型想做 X、程序却做了 Y”在结构上就很难发生：executor 没有读取模型字段的代码路径。如果你
发现自己把 `tool_call.arguments` 直接传给会碰磁盘的函数，就等于重新引入这个设计要消除的漏洞。

## Registry 固定工具词汇

`registry.py` 用每个工具的严格 Pydantic model 验证原始参数字符串，返回类型化参数，或带稳定错误码
的 `ValidationFailure`。有两个细节值得记住：

- 校验的是 provider 实际交给你的 JSON **文本**，通过 `model_validate_json` 完成，而不是先解析成 dict。
  strict 模式下，JSON 模式可能接受 tuple 字段的 JSON array，而 Python 模式会拒绝它。验证真实收到的
  形式，才能避免 `task.plan` 曾经出现的那类 bug。
- 工具集合编译进程序。测试断言 `set(ARGS_MODELS) == KNOWN_TOOLS`，也断言任何有副作用的工具都
  不能自动 `allow`。忘记分类的新工具会让构建失败，不会悄悄产生一条无保护路径。

## 错误必须是结果

工具失败返回带稳定 `error_code` 的 `ToolResult`：`unknown_tool`、`invalid_arguments`、
`denied`、`stale_preimage`、`timeout` 等。这个结果会回到模型，让它有机会修正下一步。

> 工具调用不能以异常形式冒泡进代理循环。

`tests/integration/test_tool_error_containment.py` 就是因为一次在线运行违反了这条规则：搜索不存在的
路径让 ripgrep 返回 2，异常冲出通道，整套评估被中止。修复分三层恢复了不变量：先校验路径，降低后端
故障的影响，再把执行错误包装成结果。自己构建通道时，建议先写这条测试；它能抓住最致命的一类问题：
一次工具失败拖垮整次运行。

## 练习

1. **讲述通道。** 取 `test_agent_journeys.py` 中的 `repo.edit` 流程，把
   `tool.proposed`、`policy.decided`、`approval.requested`、`execution.started`、
   `tool.completed` 分别标到产生它们的管线阶段。
2. **尝试走私参数。** 设法让 executor 执行模型提供、但 ticket 没有绑定的参数。先在纸上，再写测试，
   然后解释为什么结构上做不到。
3. **添加只读工具。** 设计一个 `repo.stat`：参数 model、registry 条目、policy 分类，以及忘记
   分类时应该失败的测试。
4. **重现隔离 bug。** 临时让 `repo.search` 在路径不存在时抛异常（不要提交），观察运行中止；再
   看当前代码怎样把它变成 `not_found` 结果。

## 自测

- `ExecutionTicket` 绑定了哪些东西？每一项为什么必要？
- 为什么要校验 JSON 文本，而不是解析后的 dict？
- 用一句话说出工具失败的不变量，并指出保护它的测试。

## 延伸阅读

- ADR 0002，以及 `docs/SECURITY.md` 的“唯一执行通道”。
- 提交 `958d98a`（`feat(application)`）引入通道与循环；`31fde25`、`95c0e78` 是隔离修复。
