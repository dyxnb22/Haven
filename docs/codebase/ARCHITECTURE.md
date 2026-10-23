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
  digests and never auto-replayed; (4) prompt text from the repo or the user's
  own config is untrusted context and can never widen policy (ADR 0031).

### 2) System Flow

```text
user goal (TUI/CLI)
  -> RunService._drive turn loop        src/haven/application/run_service.py
     -> ContextBuilder.build            src/haven/application/context_builder.py
     -> ModelPort.generate_stream       src/haven/adapters/providers/openai_compatible.py
     -> ToolPipeline.execute            src/haven/application/tool_pipeline.py
        Registry -> schema -> workspace facts -> policy -> approval (digest-bound)
        -> TOCTOU recheck -> ExecutionTicket -> executor
           (native sandbox is mandatory for exec, conditional for registered checks)
        -> evidence + journal
  -> Evidence Gate on final answer      src/haven/domain/evidence.py
  -> RunFinished + checkpoint           src/haven/adapters/sqlite_session.py
```

### 3) Layer/Module Responsibilities

| Layer or module | Owns | Must not own | Evidence |
|-----------------|------|--------------|----------|
| `domain/policy.py` | The only authority over side effects | I/O | import-linter contract |
| `application/tool_pipeline.py` | The single execution channel; per-tool dispatch tables | Provider/wire knowledge | `ToolPipeline` docstring, wiring test in `tests/unit/test_policy.py` |
| `application/run_service.py` | Bounded turn loop, budgets, steering, evidence gating | Tool execution details | module docstring (7-stage turn) |
| `application/skills.py` | Skill discovery, fail-closed parsing, catalog rendering, selection resolution, the bounded `skill.read` file reader | Tool or policy decisions; trusting skill text | `tests/unit/test_skills.py`; ADR 0031/0032 |
| `application/skill_grants.py` | Turning a selected skill's `allowed-tools` into the run's grant table | Deciding allow/ask/deny (that stays in `domain/policy.py`) | `tests/unit/test_skill_grants.py`; ADR 0032 |
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
- Skill selection is fixed per run (ADR 0031 + ADR 0008): a skill chosen
  mid-run cannot enter the active run's context, because that would move the
  cacheable prefix. The TUI accepts the selection for the *next* run and says
  so, rather than appearing to work. Selected bodies are also the first thing
  dropped when the char budget shrinks — they are advisory; system rules, the
  goal, and the run status are not.
- **A repository can grant its own writes standing approval** (ADR 0032, accepted
  by the owner). The grant can only turn ASK into ALLOW, is run-scoped and
  in-memory, and comes only from explicitly selected skills — but within those
  bounds a hostile repo removes the approval gate for itself. See
  `docs/SECURITY.md` §5a.

ADR 0030 is the cross-layer rule for the boundaries above: effect attribution
must preserve run identity, path existence, actual process lifetime, displayed
authority, and uncertainty. It is the first document to reopen when a new tool,
storage schema, or recovery outcome changes any of those facts.

### 6) Evidence

- `src/haven/bootstrap.py`
- `src/haven/application/tool_pipeline.py`, `run_service.py`
- `docs/ARCHITECTURE.md`, `docs/adr/` (current count is generated in
  `README.md` / `docs/PROJECT_CARD.md`)
