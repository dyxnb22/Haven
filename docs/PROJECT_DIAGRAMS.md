# Haven 项目学习图谱

[English](PROJECT_DIAGRAMS.en.md) | **中文**

这份文档用图的方式说明 Haven 的架构、运行流程、安全边界、证据门禁、持久化恢复和推荐学习路径。

Haven 可以先记成一句话：

> 模型负责提出建议，确定性程序负责权限、执行、记录、验证和停止。

图示以当前源码为准，主要参考：

- [`src/haven/bootstrap.py`](../src/haven/bootstrap.py)
- [`src/haven/application/run_service.py`](../src/haven/application/run_service.py)
- [`src/haven/application/tool_pipeline.py`](../src/haven/application/tool_pipeline.py)
- [`src/haven/domain/evidence.py`](../src/haven/domain/evidence.py)
- [`src/haven/application/context_builder.py`](../src/haven/application/context_builder.py)
- [`src/haven/adapters/sqlite_session.py`](../src/haven/adapters/sqlite_session.py)
- [`src/haven/application/recovery_service.py`](../src/haven/application/recovery_service.py)
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)

## 1. 总体架构

```mermaid
flowchart TB
    USER["用户<br/>本地 Git 工作区"] --> IF

    subgraph IF["接口层 interfaces"]
        CLI["CLI<br/>Typer"]
        TUI["TUI<br/>Textual"]
    end

    IF --> BOOT["bootstrap.py<br/>唯一组合根"]

    subgraph APP["应用层 application"]
        RUN["RunService<br/>Agent Loop"]
        PIPE["ToolPipeline<br/>单一执行通道"]
        CTX["ContextBuilder<br/>构造模型上下文"]
        REC["RecoveryService<br/>ReplayService"]
    end

    subgraph CORE["核心规则"]
        DOMAIN["domain<br/>Policy · Budget<br/>Evidence Gate · State"]
        PORTS["ports<br/>Model · Workspace<br/>Executor · Session"]
        CONTRACTS["contracts<br/>Pydantic DTO<br/>Events · Checkpoints"]
    end

    subgraph ADAPTERS["适配器层 adapters"]
        MODEL["OpenAI-compatible<br/>ScriptedModel"]
        FS["FsWorkspace"]
        EXEC["ProcessExecutor"]
        SANDBOX["Seatbelt / Landlock"]
        DB["SQLiteSessionStore"]
    end

    BOOT --> APP
    BOOT --> ADAPTERS

    APP --> DOMAIN
    APP --> PORTS
    APP --> CONTRACTS

    ADAPTERS --> PORTS
    ADAPTERS --> CONTRACTS
    PORTS --> DOMAIN
```

关键理解：

- `domain/` 是最纯的业务规则，不能访问文件、数据库或网络。
- `application/` 编排业务流程，但不直接依赖具体适配器。
- `adapters/` 才真正访问模型、文件系统、进程、SQLite 和操作系统沙箱。
- `bootstrap.py` 负责把抽象接口和具体实现组装起来。
- `interfaces/` 只负责接收用户输入和展示结果。

## 2. 核心业务流程

```mermaid
flowchart TD
    A["用户输入 coding goal"] --> B["RunService.run()"]
    B --> C{"预算是否仍可用?"}

    C -- 否 --> STOP1["停止<br/>预算耗尽"]
    C -- 是 --> D["处理用户 steering"]
    D --> E["ContextBuilder.build()<br/>选择上下文、压缩历史"]
    E --> F["ModelPort.generate_stream()"]

    F --> G{"模型返回什么?"}

    G -- "tool calls" --> H["逐个调用 ToolPipeline"]
    H --> I["ToolResult 追加到 transcript"]
    I --> J{"是否重复调用并陷入循环?"}

    J -- 是 --> STOP2["停止<br/>NO_PROGRESS"]
    J -- 否 --> C

    G -- "final answer" --> K["检查空回复 / 截断回复"]
    K --> L["Evidence Gate"]

    L -- "通过" --> SUCCESS["SUCCEEDED"]
    L -- "缺少 diff/check 或检查失败" --> M["向模型追加反馈"]
    M --> C

    L -- "无法验证 / 达到 nudge 上限" --> STOP3["STOPPED"]

    F -. "可恢复 Provider 错误" .-> F
    H --> CHECKPOINT["保存 Checkpoint"]
    CHECKPOINT --> C
```

一次正常的修改任务大致是：

```text
理解任务
  → 搜索文件
  → 阅读文件
  → 规划修改
  → 生成修改建议
  → 请求审批
  → 应用修改
  → 查看 diff
  → 执行测试
  → 根据失败结果继续修复
  → 通过 Evidence Gate
  → 结束运行
```

## 3. 单一工具执行通道

这是项目最重要的安全流程。

```mermaid
flowchart LR
    P["ModelResult<br/>原始 Tool Call"] --> R["Tool Registry"]
    R -- 未注册 --> ERR["结构化错误<br/>unknown_tool"]
    R -- 已注册 --> S["严格 Schema 校验"]
    S -- 失败 --> ERR2["结构化错误<br/>invalid_arguments"]

    S -- 通过 --> F["收集工作区事实<br/>路径、digest、保护路径"]
    F --> POL{"确定性 Policy"}

    POL -- deny --> ERR3["拒绝执行<br/>denied"]
    POL -- allow --> TICKET["Mint ExecutionTicket"]
    POL -- ask --> APPROVAL["精确审批<br/>digest-bound"]

    APPROVAL -- reject --> ERR4["approval_rejected"]
    APPROVAL -- approve --> TOCTOU["重新检查 preimage"]

    TOCTOU -- 文件已变化 --> ERR5["stale_preimage"]
    TOCTOU -- 未变化 --> TICKET

    TICKET --> KIND{"执行类型"}

    KIND -- "读取 / 状态" --> WS1["Workspace Adapter"]
    KIND -- "文件修改" --> WS2["原子写入<br/>执行日志"]
    KIND -- "exec / check" --> SB["Seatbelt / Landlock"]
    SB --> PROC["ProcessExecutor"]

    WS1 --> OUT["ToolResult<br/>Evidence + Trace"]
    WS2 --> OUT
    PROC --> OUT

    ERR --> OUT
    ERR2 --> OUT
    ERR3 --> OUT
    ERR4 --> OUT
    ERR5 --> OUT

    OUT --> NEXT["下一轮 Context<br/>tool_output 标记为不可信"]
```

这里有一个关键原则：

> 执行器不接受模型的原始 JSON，只接受程序创建的 `ExecutionTicket`。

## 4. 工具分类

```mermaid
flowchart TD
    ALL["已注册工具"] --> READ["只读工具<br/>自动允许"]
    ALL --> STATE["状态工具<br/>只修改 RunContext"]
    ALL --> EFFECT["副作用工具<br/>需要审批"]
    ALL --> EXEC["进程工具<br/>需要沙箱"]

    READ --> R1["repo.list"]
    READ --> R2["repo.search"]
    READ --> R3["repo.read"]
    READ --> R4["repo.diff"]

    STATE --> S1["task.plan"]

    EFFECT --> E1["repo.edit"]
    EFFECT --> E2["repo.create"]
    EFFECT --> E3["repo.delete"]
    EFFECT --> E4["repo.move"]
    EFFECT --> E5["repo.apply_patch"]
    EFFECT --> E6["repo.check"]

    EXEC --> X1["repo.exec"]
    X1 --> X2["Seatbelt / Landlock"]
```

注意：

- `repo.exec` 的输出只是观察结果，不算测试证据。
- 只有注册过的 `repo.check` 才能用于验证修改。
- `read_only` 模式下，文件修改、检查和 `repo.exec` 都会被拒绝。
- 即使用户审批，也不能绕过路径越界、保护路径和沙箱缺失等硬拒绝。

## 5. Evidence Gate

```mermaid
flowchart TD
    A["模型不再调用工具<br/>准备给出最终答案"] --> B{"本次是否修改了文件?"}

    B -- 否 --> OK1["通过<br/>no_writes"]

    B -- 是 --> C{"是否存在 check recipe?"}
    C -- 否 --> STOP1["终止<br/>verification_unavailable"]

    C -- 是 --> D{"最后一次写入后<br/>是否产生 repo.diff?"}
    D -- 否 --> NUDGE1["要求模型先运行 repo.diff"]

    D -- 是 --> E{"最后一次写入后<br/>是否运行 repo.check?"}
    E -- 否 --> NUDGE2["要求模型先运行 repo.check"]

    E -- 是 --> F{"check 是否成功?"}
    F -- 否 --> NUDGE3["回到模型继续修复"]

    F -- 是 --> G{"代码审查是否发现问题?"}
    G -- 是 --> NUDGE4["修复危险内容<br/>秘密、冲突标记、调试语句"]
    G -- 否 --> OK2["通过<br/>evidence_satisfied"]

    NUDGE1 --> LOOP["下一轮 Agent Loop"]
    NUDGE2 --> LOOP
    NUDGE3 --> LOOP
    NUDGE4 --> LOOP
    LOOP --> A
```

写文件后的成功条件是：

```text
修改存在
  + 最后一次修改之后有 diff
  + 最后一次修改之后有 check
  + check 通过
  + diff 审查通过
```

所以模型说“我已经修好了”并不能让任务成功。

## 6. State、Context、ModelResult、Trace

```mermaid
flowchart LR
    STATE["RunContext<br/>运行状态<br/>transcript · plan · usage · ledger"] --> CB["ContextBuilder"]
    CB --> REQUEST["ModelRequest<br/>本轮模型看到的内容"]

    REQUEST --> MODEL["Model"]
    MODEL --> RESULT["ModelResult<br/>本轮返回内容"]

    RESULT --> STATE
    RESULT --> PIPE["ToolPipeline"]
    PIPE --> STATE

    STATE --> CHECK["Checkpoint"]
    STATE --> EVENTS["ApplicationEvent"]
    EVENTS --> JOURNAL["SQLite Event Journal"]

    JOURNAL --> REPLAY["ReplayService"]
    REPLAY --> REDUCE["presenter.reduce()<br/>纯函数"]
    REDUCE --> VIEW["PresenterState"]
    VIEW --> TUI["TUI widgets"]

    EVENTS --> CLI["ConsoleSink"]
    EVENTS --> JSONL["JsonlEventSink"]
```

四个概念：

| 概念 | 含义 |
| --- | --- |
| `State` | 运行过程中程序掌握的状态 |
| `Context` | 当前这一轮实际发送给模型的内容 |
| `ModelResult` | 模型刚刚返回的内容 |
| `Trace` | 程序记录下来的事件历史 |

特别重要的是：

- 历史工具输出进入 Context 时会被标记为不可信。
- 项目中的 `AGENTS.md` 也被当作不可信数据。
- 程序生成的 budget、digest、Evidence 等属于可信状态。
- TUI、CLI 和 Replay 都消费同一套事件流。

## 7. 持久化与崩溃恢复

```mermaid
flowchart TD
    A["副作用工具开始执行"] --> B["execution journal<br/>记录 STARTED<br/>preimage / postimage"]
    B --> C["实际文件或进程操作"]

    C --> D["成功完成"]
    D --> E["CONFIRMED<br/>记录 Evidence"]

    C --> F["正常失败"]
    F --> G["FAILED"]

    C --> H["进程崩溃 / 中断"]
    H --> I["EFFECT_UNKNOWN"]

    I --> J["RecoveryService.inspect()"]
    J --> K["读取 Checkpoint + Execution Journal"]
    K --> L["比较当前磁盘 digest"]

    L -- 匹配 preimage --> M["not_run<br/>确认没有执行"]
    L -- 匹配 postimage --> N["confirmed<br/>确认已经完成"]
    L -- 无法证明 --> O["unknown<br/>必须人工处理"]

    M --> RESUME["允许 resume"]
    N --> RESUME
    O --> RECON["reconcile"]
    RECON --> R1["confirmed"]
    RECON --> R2["not_run"]
    RECON --> R3["abandon"]
    R3 --> FAILED["FAILED"]
```

SQLite 中主要保存：

```text
runs          运行摘要
events        追加式事件日志
checkpoints   快速恢复快照
approvals     digest 绑定的一次性审批
executions    副作用执行日志
artifacts     原始文件内容
```

恢复策略的核心是：

> 无法证明副作用是否完成时，绝不自动重放。

## 8. 推荐学习顺序

建议按下面顺序阅读，而不是一开始就从 TUI 开始：

1. [`README.md`](../README.md)：理解项目目标和核心保证。
2. [`bootstrap.py`](../src/haven/bootstrap.py)：看所有模块如何被组装。
3. [`run_service.py`](../src/haven/application/run_service.py)：理解 Agent Loop。
4. [`tool_pipeline.py`](../src/haven/application/tool_pipeline.py)：理解模型建议如何变成安全执行。
5. `domain/`：重点阅读 `policy.py`、`evidence.py`、`budget.py`、`transitions.py`、`approval.py`、`ticket.py`。
6. `ports/` 和 `adapters/`：理解依赖倒置和具体实现。
7. `events.py`、`emitter.py`、`sqlite_session.py`：理解事件、日志和恢复。
8. `interfaces/tui/`：最后理解界面如何消费事件，而不是自己执行逻辑。
9. [`course/00-from-scratch.md`](../course/00-from-scratch.md) 到 `course/capstone.md`：用课程材料系统复习。

推荐实际体验：

```bash
uv run haven eval --offline
uv run haven debug-context "fix the failing parser test"
uv run haven sessions list
uv run haven replay RUN_ID
```

最值得优先掌握的三个文件是：

```text
run_service.py       Agent 如何循环思考
tool_pipeline.py     Agent 如何被约束
evidence.py          程序如何判断任务是否真的成功
```
