# 模块 01——心智模型：提案与权威

[English](archive/en/01-mental-models.md) | **中文**

> 文件：整个系统，尤其是 `src/haven/application/state.py`、
> `src/haven/contracts/model.py`、`src/haven/contracts/events.py`
> ADR：[0002——工具执行边界](../docs/adr/0002-tool-execution-boundary.md)

## 学习目标

- 用一句话说明 Haven 的其他机制究竟在保护什么；
- 区分 **State**、**Context**、**ModelResult** 和 **Trace**，说清每个对象的所有者与生命周期；
- 根据一条信息的性质，判断它应该存在哪里，而不是把所有东西塞进对话列表。

## 本课路线

- **前置：** 模块 00 的阶段 1–2。
- **建议用时：** 45 分钟。
- **阅读顺序：** 先读 `RunContext` 的字段，再看 `ModelResult`，最后看 `EventEnvelope` 和
  `ContextBuilder.build`；不要从完整运行循环开始。
- **最小验证：**

```bash
uv run pytest -q tests/unit/test_context_builder.py tests/unit/test_transitions.py
```

## 先记住这一句话

> 模型只负责提出建议。权限、执行和“成功”的定义，属于确定性的程序代码。

模型输出：

```json
{"tool":"repo.edit","path":"/etc/passwd","new_string":"..."}
```

它没有因此获得写文件的能力，只是生成了一段可被程序拒绝的字符串。代理是执行程序，LLM 只影响其中
一部分决策。

模型输出不确定，也可能受恶意仓库文本影响；写文件和启动进程却会产生真实副作用。因此权限只能放在
可测试的代码中。

## 四个故意不合并的对象

Haven 把“对话”拆成四种所有者、生命周期和信任含义不同的对象：

| 概念 | 所有者 | 生命周期 | 内容 |
|---|---|---|---|
| **State** | `application/state.py` 的 `RunContext` | 一次运行 | 运行知道的事实：转录、用量、证据账本、已读文件、计划 |
| **Context** | `application/context_builder.py` | 一轮请求 | 模型本轮实际看到的、经过选择、预算适配和信任标注的子集 |
| **ModelResult** | `contracts/model.py` | 一轮请求 | 模型刚返回的文本、工具调用提案和用量 |
| **Trace** | `contracts/events.py` 的日志 | 持久存在 | 程序记录的发生过的事件：追加、可审计、可重放 |

打开 `RunContext` 看它没有什么：没有 HTTP client，没有文件句柄，也没有 policy。它是运行事实的
容器。再看 `ContextBuilder`：它每一轮从 State 推导 `ModelRequest`，会选择、排序、标注，而不是
把 State 原封不动倒进 prompt。

### 这种区分带来的具体收益

- **提示注入无法直接改变权限。** 恶意仓库文件属于不可信 Context，不会自动变成 State 或 policy；
- **重放不需要模型。** Trace 是独立的追加记录，`haven replay` 可以让同一个 reducer 再投影一次；
- **截断不丢计划。** 计划属于 State，ContextBuilder 每轮重新渲染它，不依赖可能被丢弃的 transcript。

这些边界让恢复、截断和安全审计互不混淆。

## 到代码里验证

- 打开 `src/haven/application/state.py`，找到 `RunContext`。逐个查看字段：它是运行知道的事实，
  还是每轮应该重新推导的 Context？确认计划在这里，而不是 transcript 中。
- 打开 `src/haven/contracts/events.py`，浏览事件类型。这里是 Trace 的词汇表。注意
  `TRANSIENT_KINDS`：它们会到达 UI，却不会持久化。为什么流式增量需要这个例外？

## 延伸阅读

- ADR 0002：本模块要建立的执行边界。
- `docs/ARCHITECTURE.md`：四种对象与分层图。
- 提交 `00139be`（`feat(contracts,ports)`）：类型化边界出现、逻辑尚未加入时的形状。
