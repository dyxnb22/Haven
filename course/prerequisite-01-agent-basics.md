# 预备课 A——从一次模型调用到 Agent

[返回课程总览](README.md) | [源码伴读索引](code-reading-map.md) |
[下一课：读懂 Haven 所需的 Python](prerequisite-02-python-reading-kit.md)

这节课建立一条最小因果链：模型接收消息、提出工具调用，程序执行并返回结果，直到明确停止；同时解释
为什么“自然停止”不等于“任务成功”。

## 本课路线

- **前置：** 会读基本 Python 函数和 JSON；不需要调用过 LLM API。
- **建议用时：** 60 分钟。
- **本课产出：** 能用自己的话区分 LLM、Provider、Chat、Workflow、Tool Calling 和 Agent。
- **完成标准：** 能在不看正文时画出 `目标 → 模型提案 → 程序校验/执行 → 观察 → 再次决策 → 验收`。

## 本课的项目锚点

这一课不使用另一个教学 Demo。所有概念都从 Haven 当前代码中取：

| 概念 | Haven 中的真实对象 | 固定行为的测试/Trace |
|---|---|---|
| 一次模型请求 | `contracts/model.py::ModelRequest` | `tests/contract/test_scripted_model.py` |
| 消息 | `ModelMessage` | `tests/unit/test_context_builder.py` |
| 工具说明 | `ToolSchema`、`contracts/tools.py::tool_schemas` | `request.envelope` 中的 tool names |
| 工具提案 | `ToolCallProposal` | `tool.proposed` |
| 流式返回 | `ModelEvent` 判别联合 | provider contract tests |
| 一轮完整结果 | `ModelResult` | `model.completed` |
| Agent Loop | `application/run_service.py::RunService._drive` | `tests/integration/test_agent_journeys.py` |
| 工具执行边界 | `ToolPipeline.execute` | `policy.decided`、`tool.completed` |
| 成功判定 | `AnswerResolver.finish_with_gate` | `run.finished.gate_reason` |

阅读时把课程与这些符号并排打开。

## 1. 先把模型想简单一点

先打开 `src/haven/contracts/model.py` 的 `ModelRequest` 和 `ModelResult`。对 Haven 来说，一次模型调用
就是从前者产生后者的不确定过程：

```text
消息 + 可用工具的说明 + 生成参数
                ↓
       文本 / 工具调用提案 / 用量 / 结束原因
```

`ModelRequest` 保存消息、工具和生成参数；`ModelResult` 保存文本、工具提案、用量、结束分类和计时。
模型没有 Haven 进程的文件、数据库或操作系统权限。它输出“请编辑 `a.py`”，不代表文件已经改变。

`ContextBuilder.build` 每轮重新生成 `ModelRequest`。模型不会自动记得上轮内容；应用必须把所需历史或
状态再次放进请求。Provider 的服务端会话不能替代应用自己的状态、权限和审计。

### 最少要认识的词

| 词 | 在本课程中的意思 | 容易混淆的地方 |
|---|---|---|
| **LLM / 模型** | 根据输入生成下一个文本或结构化片段的模型 | 它不是拥有文件权限的“用户” |
| **Provider** | 接受 HTTP 请求、托管并调用模型的服务 | Provider API 不是模型核心本身 |
| **Message** | 带 `system/user/assistant/tool` 角色的一段输入 | role 表示协议位置，不自动产生权限 |
| **Token** | 模型处理文本的计量单位 | 不等于字符或单词 |
| **Context window** | 一次请求能容纳的输入和输出总空间 | 不是长期记忆 |
| **Temperature** | 影响采样分布的参数 | 设为 0 也不保证跨时间绝对确定 |
| **Streaming** | 响应按增量事件陆续到达 | 半截文本或半截参数还不是完整结果 |
| **Schema** | 工具参数的机器可校验形状 | 合法参数不代表动作被授权 |
| **Observation** | 工具执行后返回给下一轮模型的结果 | 仍可能包含错误或恶意文本 |

读懂 Haven 先记住三点：模型输出不确定、上下文有上限、输出必须由程序验证。

### 一次请求里的消息角色

```text
system:    应用写入的稳定运行规则
user:      当前目标，以及低优先级的项目指导/补充信息
assistant: 模型之前生成的文本和工具提案
tool:      程序执行工具后返回的 Observation
```

这些是 `ModelMessage.role` 允许的四个值。role 表示协议位置，不是权限：system 规则仍需代码强制，
tool message 中的网页或仓库文本仍可能包含恶意指令。因此 Haven 同时记录 source、role 和 trust。

### 四个常见误解

- **“模型知道当前仓库。”** 它只知道本轮 Context；没有读取的文件不会凭空进入请求。
- **“返回了 JSON 就是结构化事实。”** JSON 仍可能缺字段、类型错误、越权或引用过期资源。
- **“temperature=0 就完全确定。”** 它减少采样随机性，但 Provider、模型版本和底层实现仍可能变化。
- **“模型给出的 reasoning 能证明答案。”** 推理文本也是模型输出，不是执行记录或成功证据。

## 2. Chat、Workflow 和 Agent 的区别

它们都可能使用 LLM，但控制路径不同：

| 形态 | 下一步由谁决定 | 是否执行工具 | 适合什么 |
|---|---|---|---|
| **普通 Chat** | 用户继续提问，模型生成文本 | 通常不执行 | 问答、写作、解释 |
| **Workflow** | 代码预先写好节点和分支 | 可以 | 路径稳定、合规要求高的流程 |
| **Agent** | 模型根据中间观察提出下一步，程序约束它 | 可以 | 目标明确、路径需随环境反馈变化的任务 |

本课程采用下面的定义：

> Agent 是围绕目标运行的有限循环：程序构建当前 Context，模型提出下一步，程序校验并执行动作，
> 把 Observation 写回 State，再决定继续还是以明确原因结束。

模型可以选择路径、工具和候选计划；权限、预算、执行和成功标准仍由代码掌握。固定流程优先写 Workflow；
只有中间路径需要根据环境反馈变化且收益可测时，才引入 Agent。

`_drive` 无法预先知道模型会返回 final text 还是 `result.tool_calls`；有提案时，它执行并把
`ToolResult` 加入 transcript 后继续。预算、policy、Evidence Gate 和停止原因则是确定性骨架。

## 3. Tool Calling 到底发生了什么

工具调用是一段消息协议，不是模型远程执行 Python 函数。

### 第一步：Haven 从真实参数模型生成工具说明

`contracts/tools.py::tool_schemas` 遍历 `ARGS_MODELS`，把 `RepoReadArgs` 等 Pydantic model 的 JSON
Schema 和 `TOOL_DESCRIPTIONS` 组合成 `ToolSchema`。`repo.read` 的概念形状是：

```json
{
  "name": "repo.read",
  "description": "Read a UTF-8 file inside the workspace",
  "parameters": {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"]
  }
}
```

Schema 只说明可提出的参数形状，不代表授权。

### 第二步：Provider adapter 组装 `ToolCallProposal`

```json
{
  "call_id": "c1",
  "tool_name": "repo.read",
  "arguments_json": "{\"path\":\"src/calc.py\"}"
}
```

`arguments_json` 仍是模型生成的原始文本：工具可能不存在，JSON 可能损坏，path 也可能越界。

### 第三步：`ToolPipeline.execute` 校验并执行

该方法依次调用 `ToolRegistry.validate`、`ToolFactsCollector.collect`、`evaluate_policy`、必要的
`ApprovalCoordinator.ask`、`mint_ticket` 和 `ToolExecutor.run_ticketed`。只有通过后才进入文件系统或
进程适配器。失败会变成真实的 `contracts/tools.py::ToolResult`，例如：

```json
{"status":"error","error_code":"denied","message":"path is outside workspace"}
```

### 第四步：`ToolResult.to_model_text` 变成下一轮 Observation

```json
{
  "role": "tool",
  "tool_call_id": "c1",
  "content": "{\"status\":\"ok\",\"result\":{\"path\":\"src/calc.py\",\"content\":\"...\"}}"
}
```

模型看到结果后，可以回答或提出下一步。闭环如下：

```text
tool schema → tool proposal → validate/authorize/execute → tool result → next request
```

在项目中查看这条协议留下的事件：

```bash
rg -n '"kind": "(tool.proposed|policy.decided|tool.completed)"' \
  tests/golden/data/edit_journey.json
```

## 4. 最小 Agent Loop

不要另写一个新的教学 Loop；先把 `RunService._drive` 压缩成它真实存在的控制分支：

```python
while True:
    if stop := check_budget(ctx.budget, ctx.usage):
        return await self._finish(ctx, RunStatus.STOPPED, stop, answer.final_text)

    request, segments = builder.build(ctx.transcript, ctx.usage, ctx.plan)
    result = await self._stream_model(ctx, step, request)

    if result.tool_calls:
        stopped = await self._handle_tool_calls(ctx, step, result.tool_calls, stuck)
        if stopped is not None:
            return stopped
        continue

    outcome = await self._answers.finish_with_gate(ctx, result, answer)
    if outcome is not None:
        return outcome
```

片段省略了重试、上下文恢复、checkpoint 和取消，但保留了真实入口与分支。打开源码核对：

```bash
rg -n "async def _drive|if result.tool_calls|finish_with_gate" \
  src/haven/application/run_service.py
```

需要记录的不是模型的私有思维链，而是可审计的提案、工具结果、状态变化、证据和停止原因。

朴素循环里有六个必须追问的问题：

1. 模型看到了哪些信息，哪些信息被截断了？
2. 它提出的工具和参数是否合法、被允许吗？
3. 工具失败怎样返回，是否会拖垮整个循环？
4. 循环什么时候停止，最多花多少步、时间和钱？
5. 模型停止生成工具时，任务真的成功了吗？
6. 进程在副作用中途崩溃，重启后能安全继续吗？

后续模块会依次回答这六个问题。

## 5. “停止”与“成功”是两回事

`finish_reason=stop` 只说明本轮生成自然结束；最终文本只说明模型不再提工具。两者都不能证明目标实现。

以修复代码为例：

```text
模型：“已经修好。”
```

可能对应至少四种现实：

- 它根本没有编辑文件；
- 编辑了错误文件；
- 改动存在，但测试失败；
- 改动和测试都正确。

因此系统需要两套概念：

- **StopReason：** 为什么不再继续运行，例如完成、取消、权限拒绝、无进展、预算耗尽；
- **Success evidence：** 哪些客观产物证明目标满足，例如真实 diff、最后写入后的绿测试、外部状态变化。

Haven 不允许自然语言中的“done”越过这条边界。

## 6. 一次 Coding Agent 任务的理想主线

后续课程反复使用这条主线：

```text
search → read → edit → diff → check → answer
```

| 步骤 | 模型适合决定什么 | 程序必须决定什么 |
|---|---|---|
| search/read | 去哪里找、读哪个文件 | 路径是否在工作区、结果怎样限长 |
| edit | 候选修改是什么 | 参数是否合法、是否审批、写入是否原子 |
| diff | 是否需要查看整体改动 | diff 是否来自真实工作区 |
| check | 选择已登记的验证 recipe | 命令、sandbox、超时和退出码 |
| answer | 怎样向用户解释 | 证据是否足够把运行计为成功 |

模型能力会变化；决定权放在哪里才是系统设计。

## 7. 暂不展开的主题

RAG、长期记忆、多 Agent、MCP、浏览器操作和模型训练不是理解 Haven 的前置条件：

- Haven 的主要 Context 来自当前目标、项目指导、transcript 和工具结果，不依赖向量 RAG；
- Haven 持久化运行 State 和 Trace，但不构建用户画像式长期 Memory；
- Haven 有意没有实现 subagent 和运行时 MCP，模块 10 会解释收益门禁；
- Haven 调用现成 Provider，不训练或微调模型。

模块 10 会用收益门禁判断何时值得扩展这些能力。

## 下一步

如果 `Protocol`、Pydantic、`async for`、不可变 dataclass 或 pytest 仍陌生，继续读
[预备课 B](prerequisite-02-python-reading-kit.md)；否则可直接浏览其中的项目用法。
