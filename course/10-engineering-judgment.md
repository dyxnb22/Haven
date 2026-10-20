# 模块 10——工程判断

[English](archive/en/10-engineering-judgment.md) | **中文**

> 文件：`docs/adr/`、`docs/POSTMORTEM.md`、`docs/PROJECT_CARD.md`、
> `Haven_TUI_Coding_Agent_项目计划.md`
> ADR：全部，尤其是
> [0007——没有构建什么](../docs/adr/0007-subagents-mcp-and-deterministic-review.md)

## 学习目标

- 在构建功能前执行收益门禁，并留下“为什么不构建”的记录；
- 用 ADR 记录推理，让未来的自己和审阅者看见取舍，而不只是结果；
- 把故障变成可复用的产物：症状、根因、修复和回归保护都要留下；
- 区分“范围窄但严谨”和“范围广但浅薄”，并有意识地选择。

这课代码不多，重点是如何用证据做取舍。

## 本课路线

- **前置：** 至少完成模块 03、06、09，知道能力、不变量和指标怎样互相约束。
- **建议用时：** 阅读 45 分钟，写作练习 60 分钟。
- **阅读顺序：** 选一份“构建了什么”的 ADR、一份“没有构建什么”的 ADR 和一条 postmortem，
  对比它们怎样使用证据，而不是试图一次读完整个目录。
- **最小验证：**

```bash
uv run python scripts/check_adr_links.py
uv run pytest -q tests/unit/test_adr_links.py tests/unit/test_docs_diagrams.py
```

## 先过收益门禁，再加能力

项目规则是：添加能力前先写一页，包含问题、当前基线、候选方案、决定、衡量成败的指标和回滚方式。
如果说不出可测量的收益，就还不到构建它的时候。

ADR 0007 是典型案例。两个“顶级代理”常见能力——MCP client 和模型驱动的 Reviewer subagent——都被
评估过，但**有意没有构建**：

- **MCP** 会破坏“每个工具都编译进程序且可证明地完成分类”的不变量
  （`set(ARGS_MODELS) == KNOWN_TOOLS`），而且没有改善 Haven 实际测量的指标。它被推迟，并写下
  未来重新考虑时必须满足的条件。
- **Reviewer subagent** 在线下无法通过自己的收益门禁：脚本模型只会“发现”脚本预先写好的缺陷。
  因此项目采用成本更低、结果更稳定的确定性审查（模块 06）作为备用方案。

这里的“不”是带重新评估条件的工程结论。

## ADR：把理由和决策绑在一起

读 `docs/adr/` 中的两三份 ADR。每份都有 Status、Context、Decision、Consequences 和
Alternatives-considered，既记录决定，也记录被舍弃的选项和代价。

例如，ADR 记录 `repo.create` 拒绝已有文件，是为了防止清空代理没有读过的内容。

承重选择应及时写成 ADR，并从约束它的代码和测试链接回去。

## 故障也是产物

阅读 `docs/POSTMORTEM.md`。其中三个真实缺陷都记录了症状、根因、诱人的错误修复、实际修复和回归保护：

1. 安全门禁把 `__pycache__` 当成未经授权的源文件变化。错误修复是扩大所有 allow-list；正确修复
   是在中央快照逻辑中一次性排除派生字节码。
2. 立刻构建 `debug-context` 工具暴露出 `AGENTS.md` 被错误标成 trusted 并放进 system role。
   可观测性在第一次真正使用前就证明了价值。
3. 离线测试套件因为 shell 中导出的 provider key 泄漏进 pytest，悄悄花了真实费用。修复不再列举几个
   已知变量，而是剥离所有符合 credential 形状的环境变量，并用测试保护这条保护。

共同规则是：**安全信号触发时，先检查测量是否正确；修复优先落在集中规则，而不是局部例外。**

## 范围窄但严谨，还是范围广但浅薄

Haven 有十二个编译进程序的工具、一条 provider 连接、没有 shell-string 工具，也没有多代理编排。
显式解释器仍可通过 `repo.exec` 使用，但必须审批并运行在强制 exec sandbox 中。

作为交换，系统能做出可证明的声明：离线安全门禁中的未授权变更为 0；不明确的副作用从不自动重放；
每次运行都有一个命名的停止原因。

审批 digest 可以精确绑定解释器 argv，却不能预测字符串背后程序的每一种行为；kernel profile 补上了
这个能力边界。教训不是“范围窄总是更好”，而是：

> 有意识地选择动作空间，并知道每条保证来自 schema、审批、sandbox 还是证据。

## 延伸阅读

- `docs/adr/` 中的 ADR，以及 `docs/PROJECT_CARD.md` 的取舍表。
- 原始计划 `Haven_TUI_Coding_Agent_项目计划.md` 第 10 节“风险账本”和第 11 节“收益门禁之后的增强功能”。
