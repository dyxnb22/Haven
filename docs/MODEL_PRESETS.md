# 模型预设与用量

在 `haven config explain` 输出的 `skills.user_dir` 上一级找到用户 `config.toml`。
为当前 Provider 添加一个预设即可使用。下面的 `luna` 是预设名。

```toml
[model_presets.luna]
model = "gpt-6-luna"
```

双轨预设改用 `fast_model` 和 `deep_model`，可以额外设置 `fast_turns`。
填写当前 Provider 提供的模型 ID。快轨负责探索。已发生编辑、计划开始实施或超过快轨轮数时，走深轨。
`fast_turns = 0` 让第一轮就走深轨。用户配置最多可定义 16 个预设。

```bash
haven config explain
haven run "梳理测试入口" --model-preset luna
```

`haven continue` 和 `haven implement` 同样可以为新一轮选择预设。
省略选项时使用启动时的模型或路由配置。
`--model-preset` 与 `--fast-model`、`--deep-model` 是互斥选项。
预设与默认模型共用 Provider 地址和密钥环境变量，定义放在用户配置里。

TUI 的 `/model` 展示配置、当前选择和最近一次实际调用。
`/model use luna` 将预设用于本次 TUI 会话中新启动的运行，包含追问和执行计划。
`/model clear` 恢复启动时的模型或路由。命令可以在运行中或暂停时使用。
同一运行的恢复和回答沿用检查点保存的模型及快慢路由，包含后来改名或删除的预设。
如果 Provider 地址或用户费率已变化，恢复前需要恢复原设置。暂停的回答会继续保留。
没有模型快照的旧检查点使用启动时的配置。

## 模型与费用

`haven config explain` 提供配置来源、上下文字符预算、已知的 token 窗口和推理设置。
CLI 输出、TUI Trace 和导出报告记录实际模型、选择依据、用量来源和费用。
字符预算是 Haven 组装消息的限制。模型文档的窗口和输出上限是另外两项能力。

OpenAI 官方端点上的 `gpt-4o-mini` 有内置标准文本费率。
OpenAI 已公布该模型的 2027-01-14 停用日期。
[型号参数](https://developers.openai.com/api/docs/models/gpt-4o-mini) ·
[API 型号与停用日期](https://developers.openai.com/api/reference/resources/responses/methods/create)

同一端点也支持显式选择 `gpt-6-luna`。Haven 使用 Chat Completions，函数调用时设置
`reasoning_effort = "none"`，通过 `max_completion_tokens` 限制输出。
Luna 的缓存写入和长上下文阶梯费用超出内置费率卡的表达范围，默认显示费用未知。
这两款模型的请求和工具往返已用本地模拟响应验证，账号可用性仍以实际 Provider 为准。
[GPT-6 Luna 型号参数](https://developers.openai.com/api/docs/models/gpt-6-luna) ·
[Chat Completions 参数](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

内置公开费率按官方端点和实际调用的模型匹配。兼容网关使用自己提供的费率，
可以在用户配置中通过现有的 `[pricing]` 指定。这张用户费率卡覆盖本次配置里的所有模型。
费用以已完成模型调用的用量乘费率估算，报告会标明 `estimated`。
Provider 没返回 token 数时，Haven 会从请求和输出估算并标记用量来源。
有任一调用缺少费率时，运行总费用显示 `unknown`。事件和 JSON 的 `cost_usd` 保留可计价部分，
`cost_known` 表示能否估算整次运行。费用预算依据可计价部分，步数、时间和 token 限制照常生效。

## 常见行为

| 情况 | Haven 的处理 |
|---|---|
| 认证失败 | 401 和 403 结束运行，事件标为 `auth`，隐藏认证响应正文 |
| 限流 | 按有上限的退避等待，考虑 `Retry-After`，最多重试两次 |
| 瞬态网络故障和 5xx | 同一模型上有界重试。若已经输出片段，事件标记流重启 |
| 输出截断 | 最多发起两次续写。需要原生前缀的模型还须匹配端点和路由候选，其余走对话续写 |
| 工具调用 | 组装流式工具参数并映射回工具名，通过现有审批和证据流程执行 |
| 工具推理重放 | 按模型配置重放。若首次请求在流开始前明确报出相关 400 错误，适配器尝试一次重放 |
| 上下文超限 | 仅缩小实际所用模型的字符预算，同一步最多重试两次 |
