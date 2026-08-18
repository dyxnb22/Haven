# 模块 01——心智模型：提案与权威

[English](archive/en/01-mental-models.md) | **中文**

> 文件：整个系统，尤其是 `src/haven/application/state.py`、
> `src/haven/contracts/model.py`、`src/haven/contracts/events.py`
> ADR：[0002——工具执行边界](../docs/adr/0002-tool-execution-boundary.md)

## 学习目标

学完后，你应该能够：

- 用一句话说明 Haven 的其他机制究竟在保护什么；
- 区分 **State**、**Context**、**ModelResult** 和 **Trace**，说清每个对象的所有者与生命周期；
- 根据一条信息的性质，判断它应该存在哪里，而不是把所有东西塞进对话列表。

## 先记住这一句话

> 模型只负责提出建议。权限、执行和“成功”的定义，属于确定性的程序代码。

这句话不是风格偏好，而是系统的安全边界。模型输出：

```json
{"tool":"repo.edit","path":"/etc/passwd","new_string":"..."}
```

它并没有因此获得写文件的能力，只是生成了一段程序可以拒绝的字符串。代理也不是“一个会做事的 LLM”，
而是一个会做事的程序，其中某些决定受到 LLM 提案的影响。

为什么要把边界画得这么硬？因为模型输出既非确定，又可能受到攻击者影响：仓库文件可以诱导模型去读
`~/.ssh`。写文件、启动进程这类副作用却是真实而确定的。你无法让模型本身成为可信的权限系统，
只能让它除了提出建议之外没有直接能力，再把所有权威放进能测试的代码里。

## 四个故意不合并的对象

初学者常把“对话”维护成一个大列表，让所有模块读取和修改它。Haven 把这个列表拆成四种有不同
所有者、生命周期和信任含义的对象：

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

这些区分看起来像抽象设计，直到你需要恢复、截断或解释一次安全事件，才会发现它们其实是在隔离故障。

## 到代码里验证

- 打开 `src/haven/application/state.py`，找到 `RunContext`。逐个查看字段：它是运行知道的事实，
  还是每轮应该重新推导的 Context？确认计划在这里，而不是 transcript 中。
- 打开 `src/haven/contracts/events.py`，浏览事件类型。这里是 Trace 的词汇表。注意
  `TRANSIENT_KINDS`：它们会到达 UI，却不会持久化。为什么流式增量需要这个例外？

## 练习

1. **分类。** 判断以下信息属于 State、Context、ModelResult 还是 Trace，并写出它会位于哪个文件：
   (a) 第 2 步读到的文件摘要；(b) assistant 流式输出的“我先看看 parser”；
   (c) 第 4 步 policy 拒绝 `repo.edit` 的事实；(d) 第 5 步实际发送给 provider 的完整消息集合。
2. **找边界。** 在 `src/haven` 中搜索 `httpx` 和 `aiosqlite`。哪些层导入它们，哪些层从不导入？
   用“模型只能提案”解释这种依赖方向。
3. **纸上攻击。** 假设允许模型写入 State 来“记住事情”，再让 ContextBuilder 读取它。仓库中的恶意文本
   会怎样把一段不可信输入变成下一轮的权威事实？

## 自测

- 为什么“模型说完成了”不能作为成功信号？
- 如果同事说 State 和 Context 是一回事，请用一个具体收益、两句话反驳。
- 四种对象中，谁是“发生了什么”的依据？为什么不能直接相信模型看到的 transcript？

## 延伸阅读

- ADR 0002：本模块要建立的执行边界。
- `docs/ARCHITECTURE.md`：四种对象与分层图。
- 提交 `00139be`（`feat(contracts,ports)`）：类型化边界出现、逻辑尚未加入时的形状。
