# Architecture

## Core Sections (Required)

### 1) Architectural Style

- Primary style: layered ports-and-adapters (hexagonal) around an explicit
  agent loop; event-sourced trace (append-only journal) + checkpoint snapshots.
- Why this classification: `src/haven/` is organized as
  domain/ports/contracts/application/adapters/interfaces with a single
  composition root (`bootstrap.py`), and import-linter enforces the direction
  (`pyproject.toml [tool.importlinter]`).
- Primary constraints: (1) the model may only propose — every side effect
  passes one execution channel; (2) success is decided by the Evidence Gate,
  never by model self-report; (3) interrupted effects are classified against
  digests and never auto-replayed.

### 2) System Flow

```text
user goal (TUI/CLI)
  -> RunService._drive turn loop        src/haven/application/run_service.py
     -> ContextBuilder.build            src/haven/application/context_builder.py
     -> ModelPort.generate_stream       src/haven/adapters/providers/openai_compatible.py
     -> ToolPipeline.execute            src/haven/application/tool_pipeline.py
        Registry -> schema -> workspace facts -> policy -> approval (digest-bound)
        -> TOCTOU recheck -> ExecutionTicket -> sandboxed executor -> evidence + journal
  -> Evidence Gate on final answer      src/haven/domain/evidence.py
  -> RunFinished + checkpoint           src/haven/adapters/sqlite_session.py
```

### 3) Layer/Module Responsibilities

| Layer or module | Owns | Must not own | Evidence |
|-----------------|------|--------------|----------|
| `domain/policy.py` | The only authority over side effects | I/O | import-linter contract |
| `application/tool_pipeline.py` | The single execution channel; per-tool dispatch tables | Provider/wire knowledge | `ToolPipeline` docstring, wiring test in `tests/unit/test_policy.py` |
| `application/run_service.py` | Bounded turn loop, budgets, steering, evidence gating | Tool execution details | module docstring (7-stage turn) |
| `adapters/workspace_fs.py` | Path confinement, preview/preimage binding, atomic writes, patch transaction | Policy decisions | class docstring invariants |
| `adapters/sqlite_session.py` + `memory_session.py` | Durable journal/checkpoints/approvals/executions/artifacts | Business rules | `tests/contract/test_session_store.py` keeps them equal |
| `interfaces/` (CLI/TUI) | Presentation, service calls | Executing tools, policy | `SessionServices` protocol in `tui/app.py` |

### 4) Reused Patterns

| Pattern | Where found | Why it exists |
|---------|-------------|---------------|
| Ports & adapters (Protocol) | `ports/*.py`, `adapters/*` | Swap SQLite/memory, live/scripted model without touching core |
| Event sourcing + snapshot | `contracts/events.py`, `contracts/checkpoint.py` | Replay/forensics + fast resume |
| Dispatch table per tool | `ToolPipeline._facts_handlers/_execute_handlers` | Kill 12-way isinstance chains; wiring pinned by test |
| Digest-bound single-use grant | `domain/approval.py`, consume via conditional SQL UPDATE | Approval cannot be replayed or drift |
| Pure reducer for UI | `interfaces/tui/presenter.py::reduce` | TUI state derivable from the journal (replay = same view) |
| Composition root | `bootstrap.py::build_services` | One place that knows adapters and use cases |

### 5) Known Architectural Risks

- Single-run-per-service assumption: `RunService` keeps `_active_run_id` and
  `_steer_queue` as instance state — concurrent runs in one process are not
  supported (documented; lease covers cross-process).
- Three parallel event renderers (TUI presenter, `ConsoleSink`,
  `interfaces/export.py`) must be updated together for new event kinds; no
  shared mapping layer (accepted trade-off, small).
- Char-based context budget calibrated to tokens (ADR 0022) — measured safe,
  but a provider/tokenizer change requires recalibration
  (`evals/calibrate_context.py`).

### 6) Evidence

- `src/haven/bootstrap.py`
- `src/haven/application/tool_pipeline.py`, `run_service.py`
- `docs/ARCHITECTURE.md`, `docs/adr/` (25 ADRs)
