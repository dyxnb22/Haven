# Haven Project Learning Diagrams

**English** | [中文](PROJECT_DIAGRAMS.md)

This document explains Haven's architecture, runtime flow, security boundaries, Evidence Gate, persistence and recovery, and recommended learning path through diagrams.

A useful one-sentence model of Haven is:

> The model proposes; deterministic program code owns permission, execution, records, verification and stopping.

The diagrams follow the current source. Main references:

- [`src/haven/bootstrap.py`](../src/haven/bootstrap.py)
- [`src/haven/application/run_service.py`](../src/haven/application/run_service.py)
- [`src/haven/application/tool_pipeline.py`](../src/haven/application/tool_pipeline.py)
- [`src/haven/domain/evidence.py`](../src/haven/domain/evidence.py)
- [`src/haven/application/context_builder.py`](../src/haven/application/context_builder.py)
- [`src/haven/adapters/sqlite_session.py`](../src/haven/adapters/sqlite_session.py)
- [`src/haven/application/recovery_service.py`](../src/haven/application/recovery_service.py)
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)

## 1. Overall architecture

```mermaid
flowchart TB
    USER["User<br/>local Git workspace"] --> IF

    subgraph IF["interfaces"]
        CLI["CLI<br/>Typer"]
        TUI["TUI<br/>Textual"]
    end

    IF --> BOOT["bootstrap.py<br/>only composition root"]

    subgraph APP["application"]
        RUN["RunService<br/>Agent Loop"]
        PIPE["ToolPipeline<br/>single execution channel"]
        CTX["ContextBuilder<br/>builds model context"]
        REC["RecoveryService<br/>ReplayService"]
    end

    subgraph CORE["core rules"]
        DOMAIN["domain<br/>Policy · Budget<br/>Evidence Gate · State"]
        PORTS["ports<br/>Model · Workspace<br/>Executor · Session"]
        CONTRACTS["contracts<br/>Pydantic DTO<br/>Events · Checkpoints"]
    end

    subgraph ADAPTERS["adapters"]
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

Key takeaways:

- `domain/` contains the pure business rules and cannot access files, databases or networks.
- `application/` orchestrates the business flow without depending on concrete adapters.
- `adapters/` are where the model, filesystem, processes, SQLite and OS sandbox are actually accessed.
- `bootstrap.py` assembles abstractions and concrete implementations.
- `interfaces/` only receives user input and presents results.

## 2. Core business flow

```mermaid
flowchart TD
    A["User enters coding goal"] --> B["RunService.run()"]
    B --> C{"Budget still available?"}

    C -- no --> STOP1["Stop<br/>budget exhausted"]
    C -- yes --> D["Process user steering"]
    D --> E["ContextBuilder.build()<br/>select context, compact history"]
    E --> F["ModelPort.generate_stream()"]

    F --> G{"What did the model return?"}

    G -- "tool calls" --> H["Call ToolPipeline for each"]
    H --> I["Append ToolResult to transcript"]
    I --> J{"Repeated call / stuck loop?"}

    J -- yes --> STOP2["Stop<br/>NO_PROGRESS"]
    J -- no --> C

    G -- "final answer" --> K["Check empty / truncated answer"]
    K --> L["Evidence Gate"]

    L -- pass --> SUCCESS["SUCCEEDED"]
    L -- "missing diff/check or failed check" --> M["Feed feedback to model"]
    M --> C
    L -- "cannot verify / nudge limit reached" --> STOP3["STOPPED"]

    F -. "recoverable Provider error" .-> F
    H --> CHECKPOINT["Save Checkpoint"]
    CHECKPOINT --> C
```

A normal modification task looks like:

```text
Understand the task
  → search files
  → read files
  → plan the change
  → propose the edit
  → request approval
  → apply the edit
  → inspect the diff
  → run tests
  → continue fixing from failures
  → pass the Evidence Gate
  → finish the run
```

## 3. Single tool execution channel

This is the project's most important security flow.

```mermaid
flowchart LR
    P["ModelResult<br/>raw tool call"] --> R["Tool Registry"]
    R -- unregistered --> ERR["Structured error<br/>unknown_tool"]
    R -- registered --> S["Strict Schema validation"]
    S -- failed --> ERR2["Structured error<br/>invalid_arguments"]

    S -- passed --> F["Collect workspace facts<br/>path, digest, protected paths"]
    F --> POL{"Deterministic Policy"}

    POL -- deny --> ERR3["Deny execution<br/>denied"]
    POL -- allow --> TICKET["Mint ExecutionTicket"]
    POL -- ask --> APPROVAL["Exact approval<br/>digest-bound"]

    APPROVAL -- reject --> ERR4["approval_rejected"]
    APPROVAL -- approve --> TOCTOU["Re-check preimage"]

    TOCTOU -- changed --> ERR5["stale_preimage"]
    TOCTOU -- unchanged --> TICKET

    TICKET --> KIND{"Execution kind"}

    KIND -- "read / state" --> WS1["Workspace Adapter"]
    KIND -- "file edit" --> WS2["Atomic write<br/>execution journal"]
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
    OUT --> NEXT["Next Context<br/>tool_output marked untrusted"]
```

The key principle is:

> The executor does not accept raw model JSON; it accepts only a program-created `ExecutionTicket`.

## 4. Tool categories

```mermaid
flowchart TD
    ALL["Registered tools"] --> READ["Read-only tools<br/>auto-allowed"]
    ALL --> STATE["State tools<br/>change only RunContext"]
    ALL --> EFFECT["Effect tools<br/>approval required"]
    ALL --> EXEC["Process tools<br/>sandbox required"]

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

Important details:

- `repo.exec` output is observation, not test evidence.
- Only a registered `repo.check` can verify a modification.
- In `read_only` mode, file edits, checks and `repo.exec` are denied.
- Even user approval cannot bypass hard denials such as path escape, protected paths or a missing sandbox.

## 5. Evidence Gate

```mermaid
flowchart TD
    A["Model stops calling tools<br/>ready to answer"] --> B{"Did this run modify files?"}
    B -- no --> OK1["Pass<br/>no_writes"]
    B -- yes --> C{"Is there a check recipe?"}
    C -- no --> STOP1["Stop<br/>verification_unavailable"]
    C -- yes --> D{"Did repo.diff run after<br/>the last write?"}
    D -- no --> NUDGE1["Ask model to run repo.diff"]
    D -- yes --> E{"Did repo.check run after<br/>the last write?"}
    E -- no --> NUDGE2["Ask model to run repo.check"]
    E -- yes --> F{"Did the check pass?"}
    F -- no --> NUDGE3["Return to model and continue fixing"]
    F -- yes --> G{"Did deterministic review find a problem?"}
    G -- yes --> NUDGE4["Fix unsafe content<br/>secrets, conflict markers, debug statements"]
    G -- no --> OK2["Pass<br/>evidence_satisfied"]
    NUDGE1 --> LOOP["Next Agent Loop turn"]
    NUDGE2 --> LOOP
    NUDGE3 --> LOOP
    NUDGE4 --> LOOP
    LOOP --> A
```

After a file write, success requires:

```text
the change exists
  + a diff exists after the last write
  + a check ran after the last write
  + the check passed
  + deterministic diff review passed
```

Therefore, “I fixed it” in the model's answer cannot make the task succeed.

## 6. State, Context, ModelResult and Trace

```mermaid
flowchart LR
    STATE["RunContext<br/>run state<br/>transcript · plan · usage · ledger"] --> CB["ContextBuilder"]
    CB --> REQUEST["ModelRequest<br/>what the model sees this turn"]
    REQUEST --> MODEL["Model"]
    MODEL --> RESULT["ModelResult<br/>what the model returned"]
    RESULT --> STATE
    RESULT --> PIPE["ToolPipeline"]
    PIPE --> STATE
    STATE --> CHECK["Checkpoint"]
    STATE --> EVENTS["ApplicationEvent"]
    EVENTS --> JOURNAL["SQLite Event Journal"]
    JOURNAL --> REPLAY["ReplayService"]
    REPLAY --> REDUCE["presenter.reduce()<br/>pure function"]
    REDUCE --> VIEW["PresenterState"]
    VIEW --> TUI["TUI widgets"]
    EVENTS --> CLI["ConsoleSink"]
    EVENTS --> JSONL["JsonlEventSink"]
```

| Concept | Meaning |
|---|---|
| `State` | State the program knows during the run |
| `Context` | Content actually sent to the model this turn |
| `ModelResult` | Content just returned by the model |
| `Trace` | Event history recorded by the program |

Especially important:

- Historical tool output is marked untrusted when it enters Context.
- `AGENTS.md` content in the repository is also treated as untrusted data.
- Program-generated budget, digest and Evidence are trusted state.
- TUI, CLI and Replay consume the same event stream.

## 7. Persistence and crash recovery

```mermaid
flowchart TD
    A["Effect tool starts"] --> B["Execution journal<br/>record STARTED<br/>preimage / postimage"]
    B --> C["Actual file or process operation"]
    C --> D["Completed successfully"]
    D --> E["CONFIRMED<br/>record Evidence"]
    C --> F["Normal failure"]
    F --> G["FAILED"]
    C --> H["Process crash / interruption"]
    H --> I["EFFECT_UNKNOWN"]
    I --> J["RecoveryService.inspect()"]
    J --> K["Read Checkpoint + Execution Journal"]
    K --> L["Compare current disk digest"]
    L -- matches preimage --> M["not_run<br/>confirm it did not execute"]
    L -- matches postimage --> N["confirmed<br/>confirm it completed"]
    L -- cannot prove --> O["unknown<br/>manual handling required"]
    M --> RESUME["Allow resume"]
    N --> RESUME
    O --> RECON["reconcile"]
    RECON --> R1["confirmed"]
    RECON --> R2["not_run"]
    RECON --> R3["abandon"]
    R3 --> FAILED["FAILED"]
```

SQLite mainly stores:

```text
runs          run summary
events        append-only event journal
checkpoints   fast-resume snapshots
approvals     digest-bound, single-use approvals
executions    effect execution journal
artifacts     original file content
```

The core recovery rule is:

> When Haven cannot prove whether an effect completed, it never replays it automatically.

## 8. Recommended learning order

Read in this order instead of starting with the TUI:

1. [`README.md`](../README.md): understand the product goal and guarantees.
2. [`bootstrap.py`](../src/haven/bootstrap.py): see how modules are assembled.
3. [`run_service.py`](../src/haven/application/run_service.py): understand the Agent Loop.
4. [`tool_pipeline.py`](../src/haven/application/tool_pipeline.py): understand how proposals become safe execution.
5. `domain/`: focus on `policy.py`, `evidence.py`, `budget.py`, `transitions.py`, `approval.py` and `ticket.py`.
6. `ports/` and `adapters/`: understand dependency inversion and concrete implementations.
7. `events.py`, `emitter.py` and `sqlite_session.py`: understand events, logs and recovery.
8. `interfaces/tui/`: finally see how the interface consumes events rather than executing logic.
9. [`course/00-from-scratch.md`](../course/00-from-scratch.md) through `course/capstone.md`: review the system with the guided course.

Recommended commands to try:

```bash
uv run haven eval --offline
uv run haven debug-context "fix the failing parser test"
uv run haven sessions list
uv run haven replay RUN_ID
```

The three files to prioritize are:

```text
run_service.py       how the Agent loops
tool_pipeline.py     how the Agent is constrained
evidence.py          how the program decides the task really succeeded
```
