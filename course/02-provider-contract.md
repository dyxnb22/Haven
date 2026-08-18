# 模块 02——Provider 契约与流式传输

[English](archive/en/02-provider-contract.md) | **中文**

> 文件：`src/haven/contracts/model.py`、`src/haven/ports/model.py`、
> `src/haven/adapters/providers/openai_compatible.py`、
> `src/haven/adapters/providers/scripted.py`
> 测试：`tests/contract/test_openai_compatible.py`、`tests/contract/test_scripted_model.py`
> ADR：[0001——语言与范围](../docs/adr/0001-language-and-scope.md)

## 学习目标

- 设计与 provider 无关的模型契约，把线格式细节挡在核心之外；
- 正确处理文本、工具调用、用量、结束事件、超时、大小限制和取消；
- 用脚本模型让整套系统可以离线、免费、确定性地测试；
- 理解 fake 为什么是承重结构，而不是测试捷径。

## 先把 provider 关在适配器里

代理不应该知道自己正在和哪家 provider 对话。一旦 OpenAI 的字段名渗进循环，换 provider 会变难，
离线测试会被迫模拟 HTTP 细节，provider 的怪癖也会变成核心 bug。

Haven 用三层把边界画清楚：

- **中立契约**（`contracts/model.py`）：`ModelRequest`、由 `TextDelta`、
  `ReasoningDelta`、`ToolCallReady`、`UsageReport`、`StreamFinished` 等事件组成的判别联合，
  以及组装后的 `ModelResult`。这里不应该出现“OpenAI”。
- **端口**（`ports/model.py`）：核心拥有的 `ModelPort` Protocol，提供
  `generate_stream(request) -> AsyncIterator[ModelEvent]`，以及带稳定错误码的 `ProviderError`。
- **适配器**（`adapters/providers/`）：只有这里处理 SSE、`choices[].delta` 或 DeepSeek 的
  cache 字段。脚本模型也实现同一个端口，只是把预先写好的事件重新播放出来。

核心依赖端口，而不是某个 provider 的 SDK；这使“换 provider”和“离线测试”成为局部工作。

## 真正危险的地方在流式传输

阅读 `openai_compatible.py` 的 `_stream`。除了把字节解析成事件，它还必须负责：

- **首事件超时与总超时。** provider 接受请求后可能一直沉默；首个 token 应有更紧的截止时间，
  总流也必须有上限。
- **响应大小上限。** 异常的流不能无限吞噬内存。
- **稳定的错误映射。** 401/403 归为 `auth`，429 归为 `rate_limited`，5xx 归为 `server`，
  格式错误归为 `protocol`。核心不应依赖原始 provider body；唯一的刻意例外是，非认证类 4xx
  可以保留一段有上限的 body，因为它通常能告诉你请求格式哪里错了。认证失败绝不回显 body。
- **凭据不外泄。** API key 不得出现在错误、日志或 Trace 中，测试会明确断言这一点。

## 脚本模型不是备用方案

`ScriptedModel` 从普通 JSON 读取一组 turn，每个 turn 又是一组 `ModelEvent`，然后按顺序重放。
因此整套测试和离线评估都不需要网络，也不消耗 token，而且每次运行都能复现。

这里的设计教训是：一开始就把 fake 当作一等公民。如果事后才补，核心代码已经到处假设某个真实
provider 的行为，到那时再做离线替身会非常痛苦。

## 两个只有真实模型才会暴露的怪癖

这两件事都是把 Haven 接到 DeepSeek 等真实推理模型后才发现的，详情在 `docs/EVAL_LIVE.md`：

1. **隐藏推理。** 模型在 answer `content` 之前发送 `reasoning_content`。粗糙的适配器会把它丢掉，
   于是报告“0 个字符”，却仍然计费输出 token。Haven 将其作为独立的 `ReasoningDelta` 送到 UI，
   但不放进 `ModelResult.text` 或 transcript：推理不是答案，而且多数 provider 不接受把自己的推理
   再作为输入。
2. **带命名空间的工具名被拒绝。** API 对函数名限制为 `^[a-zA-Z0-9_-]+$`，所以 `repo.read`
   会返回 400。点号是核心词汇的一部分，适配器负责在请求中使用 `repo__read`，并维护本次请求的
   精确反向映射。核心不必为线格式的限制改名。

## 练习

1. **跟一条事件。** 在 `openai_compatible.py` 中跟踪一行 SSE 从字节到 `ModelEvent` 的过程。
   如果某 provider 一次性发送完整的工具参数，而不是增量发送，你会在哪一层处理？
2. **编写一个 turn。** 为 `ScriptedModel` 写一组 JSON：先输出文本，再提出 `repo.read`，最后结束。
   按 `tests/contract/test_scripted_model.py` 的方式运行它。
3. **写失败测试。** 使用 `respx` 或 `httpx.MockTransport`，断言 429 映射到 `rate_limited`，
   并确认伪造的 API key 不会出现在错误中。
4. **可选在线练习。** 配置 key 后运行 `uv run haven verify-provider --yes`，观察 TTFT、用量，
   以及推理模型的 reasoning/answer 字符拆分。

## 自测

- 为什么 `ModelPort` 应由核心拥有，而不是从某个 adapter 导入？
- 如果同事说“直接把 OpenAI response object 传遍系统更简单”，它会怎样伤害测试和 provider 替换？
- 流中途失败为什么不能和首事件之前失败一视同仁？模块 05 会把重试边界讲清楚。

## 延伸阅读

- ADR 0001：技术栈，以及“单一真实 provider + fake”的取舍。
- ADR 0008 与模块 09：适配器怎样报告 cache 命中。
- 提交 `3727fb0`（`feat(providers)`）：整层实现的垂直切片。
