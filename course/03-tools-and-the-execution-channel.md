# 模块 03——工具与唯一执行通道

> 文件：`src/haven/application/tool_pipeline.py`、`src/haven/application/registry.py`、
> `src/haven/contracts/tools.py`、`src/haven/domain/ticket.py`、`src/haven/domain/policy.py`
> 测试：`tests/integration/test_agent_journeys.py`、`tests/integration/test_tool_error_containment.py`
> ADR：[0002——工具执行边界](../docs/adr/0002-tool-execution-boundary.md)

## 学习目标

- 建立一条所有模型提案都必须经过的管线；
- 能指出每个门检查什么、拒绝什么；
- 解释为什么 executor 只能接收程序签发的 **ticket**，不能接收模型原始 JSON；
- 让工具失败变成结构化结果，而不是抛进代理循环的异常。

## 本课路线

- **前置：** 模块 01；模块 02 只需知道模型输出的是 `ModelResult`。
- **建议用时：** 60 分钟。
- **阅读顺序：** 先跑一个工具失败测试，再沿 `ToolPipeline.execute` 从 validation 一直追到
  `ToolResult`；只在遇到调用时进入 registry、policy 和 executor。
- **最小验证：**

```bash
uv run pytest -q tests/integration/test_tool_error_containment.py tests/unit/test_policy.py
```

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

从上到下阅读 `ToolPipeline.execute`，为每个箭头标出输入、输出和失败码。

## 为什么需要 ExecutionTicket

`workspace_fs.py` 和 `process_executor.py` 中的 executor 永远看不到模型 JSON。管线会签发一个
`ExecutionTicket`，将工具名和版本、规范化参数、工作区身份、preimage digest 绑定成一个程序生成的值；
executor 只消费这个值。

executor 没有读取模型字段的路径，因此不能把 `tool_call.arguments` 直接传给磁盘或进程函数。

## Registry 固定工具词汇

`registry.py` 用每个工具的严格 Pydantic model 验证原始参数字符串，返回类型化参数，或带稳定错误码
的 `ValidationFailure`。有两个细节值得记住：

- 校验的是 provider 实际交给你的 JSON **文本**，通过 `model_validate_json` 完成，而不是先解析成 dict。
  strict 模式下，JSON 模式可能接受 tuple 字段的 JSON array，而 Python 模式会拒绝它。验证真实收到的
  形式，才能避免 `task.plan` 曾经出现的那类 bug。
- 工具集合编译进程序。测试断言 `set(ARGS_MODELS) == KNOWN_TOOLS`，也断言任何有副作用的工具都
  不能自动 `allow`。忘记分类的新工具会让构建失败，不会悄悄产生一条无保护路径。
- 工具数量会增长，但增长方式不变。从 v1 的十来个到现在的 25 个（数量以
  `docs/ARCHITECTURE.md` 的生成表为准，不在这里手写），每一个都走同五件套：参数 model、
  policy 分类、facts handler、execute handler、审批卡。外部 MCP 工具是唯一按命名空间动态
  出现的一类（`mcp.<server>.<tool>`），而且只在硬化网关下：静态声明、逐工具 allowlist、
  fail-closed 的 schema 钉住（ADR 0039）。

## 工具家族：同一个通道，不同的副作用形状

25 个工具按副作用形状分家族。它们走同一条通道，差别只在于 policy 分类、facts 要采集什么、
executor 落到哪种副作用：

| 家族 | 工具 | 形状 |
|---|---|---|
| 读仓库 | `repo.list`/`search`/`read`/`outline`/`symbol`/`diff` | 只读；`read` 记录后续绑定编辑的 digest |
| 改仓库 | `repo.edit`/`create`/`delete`/`move`/`apply_patch` | preimage 绑定；`apply_patch` 是多文件单次审批事务 |
| 跑进程 | `repo.exec`/`repo.check`/`task.start` | 固定 argv 过沙箱；`repo.check` 只认注册配方 |
| 后台任务 | `task.status`/`logs`/`stop`/`input` | 有界环形缓冲，`task.input` 走审批管道，`terminal=true` 用 PTY |
| 查文档 | `web.search`/`web.fetch` | 只读、字节上限、SSRF 防护，结果按不可信文本框架 |
| 验证 | `recipe.propose` | 临时验证神谕提议，argv 与超时有界、无网络、需显式审批 |
| 委派 | `agent.delegate` | 一层只读子循环，工具集白名单锁死 |
| 运行状态 | `task.plan`/`user.ask`/`task.stop` | 只动运行状态，无外部副作用 |
| 技能 | `skill.read` | 只读技能自己的目录，符号链接拒绝 |
| 外部 | `mcp.<server>.<tool>` | 唯一动态命名空间的一类，只在硬化网关下出现 |

值得注意的是最后两行的边界：`skill.read` 被限制在技能自己的目录里，外部 MCP 工具则
必须经过静态声明和 schema 钉住。两者都在回答同一个问题——“外部内容能进来多少”，
而答案是：可以作为不可信文本进来，不能作为权限进来。

## 错误必须是结果

工具失败返回带稳定 `error_code` 的 `ToolResult`：`unknown_tool`、`invalid_arguments`、
`denied`、`stale_preimage`、`timeout` 等。这个结果会回到模型，让它有机会修正下一步。

> 工具调用不能以异常形式冒泡进代理循环。

`tests/integration/test_tool_error_containment.py` 就是因为一次在线运行违反了这条规则：搜索不存在的
路径让 ripgrep 返回 2，异常冲出通道，整套评估被中止。修复分三层恢复了不变量：先校验路径，降低后端
故障的影响，再把执行错误包装成结果。自己构建通道时，建议先写这条测试；它能抓住最致命的一类问题：
一次工具失败拖垮整次运行。

## 延伸阅读

- ADR 0002，以及 `docs/SECURITY.md` 的“唯一执行通道”。
- 提交 `958d98a`（`feat(application)`）引入通道与循环；`31fde25`、`95c0e78` 是隔离修复。
