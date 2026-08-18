"""Haven：一个以证据为驱动、可重放且作用域限定在本地的编程代理。

一句话概括：模型只能“提出”操作；权限、执行以及成功定义都由确定性代码负责。

分层结构（由 import-linter 强制执行；见 pyproject `[tool.importlinter]`）
=========================================================================

    interfaces/   CLI（Typer）和 TUI（Textual）。将用户意图转换为服务调用并渲染事件，
                  绝不导入 adapters。
    bootstrap.py  组合根——唯一负责将 adapters 接入应用服务的模块。
    application/  用例层。两个主要编排器位于此处：run_service.py（代理循环）和
                  tool_pipeline.py（唯一执行通道），以及 context_builder、compaction、
                  recovery、replay、approvals、registry。
    domain/       纯逻辑，不执行 I/O：策略、审批摘要、预算、证据门禁、票据、
                  状态转换和审查。
    ports/        核心依赖的协议：model、workspace、executor、sandbox、会话存储、
                  event sink、clock。
    adapters/     ports 的实现：OpenAI 兼容提供商、文件系统工作区、进程执行器、
                  操作系统沙箱启动器（Seatbelt/Landlock）、SQLite 会话存储、
                  工作区写入租约。
    contracts/    穿过各边界的严格 Pydantic DTO：工具参数/结果、模型消息、事件、检查点。
    evalkit/      离线评估 harness（ScriptedModel + 真实 adapters）。

安全主线（分层采用这种形态的原因）
====================================

模型提出的每个副作用都必须经过唯一的通道（application/tool_pipeline.py）：

    Registry -> 严格模式 -> 工作区事实 -> 确定性策略
    -> 精确审批（绑定摘要、单次使用）-> TOCTOU 再检查
    -> ExecutionTicket -> 沙箱执行器 -> 证据 + 日志

只有当证据门禁（domain/evidence.py）看到了 diff，以及在最后一次写入之后记录的
通过状态 check 时，一次运行才能“成功”。模型文本永远不是证据。文档见
docs/SECURITY.md、docs/adr/。

业务流程及其入口
================

交互会话            interfaces/cli.py 的 `tui`（默认命令）
                    -> bootstrap.build_services -> interfaces/tui/app.py
                    -> RunService.start_run / continue_run；审批以模态卡片出现；
                    运行活跃期间输入的内容会排队，等待下一轮边界处理。
无头运行            interfaces/cli.py 的 `run`（默认只读；无人值守修复使用
                    --write + --approval-policy，机器消费使用 --jsonl / --events）。
会话 / 取证          `sessions list|show`、`replay`、`export`、`debug-context`——
                    对日志的纯投影。
崩溃恢复            `resume` -> application/recovery_service.py 按摘要对中断副作用分类；
                    无法证明的情况会阻塞，直到执行 `reconcile`。用户级撤销使用
                    `rewind`（失败即拒绝的补偿操作）。
配方发现            `discover [--accept]` -> domain/discovery.py 从仓库自身文件中
                    提议 verify 命令；`init` 将其与环境摘要组合，用于首次接触仓库。
存储维护            `gc` -> application/maintenance.py 清理旧运行和未引用构件
                   （默认只进行 dry run）。
离线评估            `eval --offline` -> evalkit/runner.py；在线套件位于 evals/（见
                    docs/EVAL_LIVE.md）。

首次阅读建议顺序
================

    1. domain/policy.py + domain/approval.py   （权限模型）
    2. application/tool_pipeline.py            （执行通道）
    3. application/run_service.py              （代理循环）
    4. application/context_builder.py          （模型能看到什么）
    5. domain/evidence.py                      （“成功”意味着什么）
    6. adapters/workspace_fs.py                （写入实际上如何落盘）
    7. interfaces/tui/app.py + presenter.py    （如何呈现给用户）
"""

__version__ = "0.1.0"
