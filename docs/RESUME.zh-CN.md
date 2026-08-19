# Haven 项目简历材料

> 这份内容用于直接拼接到中文简历；下列数字来自 2026-08-20 的最终全量验证，可在项目根目录复现。

## 推荐写法

**Haven｜证据驱动、可恢复的本地 Coding Agent（独立项目）**  
Python 3.12 · asyncio · Textual · Typer · Pydantic v2 · HTTPX · SQLite · uv

- 从零实现有界 Agent Loop 与流式 OpenAI-compatible Provider 适配层，支持搜索、读取、原子编辑、验证、diff、会话恢复及 TUI/CLI 双入口；用步骤、工具、时间、Token 和成本预算约束非确定性模型行为。
- 设计 `Registry → Schema → Facts → Policy → Approval → Ticket → Sandbox → Executor` 单一执行通道：模型只能提出动作，程序负责参数校验、权限判定和执行；审批与工作区、规范化参数、文件前镜像及预览摘要绑定，并通过条件更新保证单次消费。
- 构建基于 SQLite 检查点、追加式事件日志和 `(run_id, call_id)` 执行日志的恢复机制；结合文件前后摘要区分“未执行、已成功、已失败、影响未知”，对无法证明的副作用停止恢复并要求人工处理，避免危险的自动重放。
- 实现 Evidence Gate，将“完成”定义为最后一次写入之后存在非空 diff、注册检查通过且新增内容经过确定性审查；配套 macOS Seatbelt / Linux Landlock 沙箱、工作区路径约束、进程组回收和可复现离线安全评测。
- 建立 `ruff`、`mypy --strict`、import-linter、完整自动化测试和离线评测质量门禁；最终验证 **885 项测试全部通过、`src/` 行覆盖率 89%、102 个模块通过严格类型检查、39/39 离线评测通过且 0 安全违规**。

## 30 秒面试介绍

Haven 不是把大模型接到 shell 上，而是把大模型限制为“不可信的动作提议者”。所有读写和进程操作都进入同一条确定性执行通道，由程序收集事实、判定策略、绑定审批、执行沙箱并写入日志。修改完成后，模型说“完成”不算成功，系统必须看到最后一次写入之后的 diff、通过的注册检查和确定性代码审查。进程中断时再利用前后摘要恢复；只要副作用无法证明，就标记为 `EFFECT_UNKNOWN`，绝不自动重放。

## 建议重点准备的追问

- 为什么工具调用只是提议，不是权限？审批摘要具体绑定了哪些事实，如何防止 TOCTOU？
- 为什么事件日志、检查点和执行日志缺一不可？`EFFECT_UNKNOWN` 在什么情况下产生？
- Evidence Gate 如何避免旧测试结果、空 diff 或模型自报完成被误判为成功？
- Seatbelt 与 Landlock 的能力差异是什么？为什么注册检查仍有“本地可信仓库”边界？
- 为什么选择显式状态机和端口适配架构，而没有使用 Agent 框架、MCP 或多 Agent？

详细证据和权衡分别见 [`PROJECT_CARD.md`](PROJECT_CARD.md)、[`DESIGN_QA.md`](DESIGN_QA.md)、[`SECURITY.md`](SECURITY.md) 与 [`ADR_INDEX.md`](ADR_INDEX.md)。
