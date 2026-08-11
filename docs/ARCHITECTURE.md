# Haven Architecture

## System context

Haven runs inside a user-trusted local Git workspace. The model proposes text and tool calls; Haven validates, authorizes, executes, records evidence, and decides when a run succeeds or stops.

## Trust boundaries

| Component | Role | Must not |
|---|---|---|
| User | Goal, approval, cancel, resume | Bypass deterministic policy via natural language |
| TUI / CLI | UserIntent in, ApplicationEvent out | Execute tools or own permission rules |
| AgentLoop | Orchestrate model and next step | Read files, spawn processes, or write persistence directly |
| Model | Propose text or tool calls | Approve itself or cause side effects directly |
| ToolPipeline | Registry, schema, policy, approval, execution | Skip any gate |
| Workspace / Executor | Authorized I/O only | Accept raw model JSON |
| EvidenceGate | Decide success from diff and checks | Accept model text as sole success evidence |
| SessionStore | Events, checkpoints, approvals | Store secrets or unbounded raw content |

## Layering

```text
interfaces ──> application ──> domain
     │              │             ▲
     │              └──> ports ───┘
     │
bootstrap ──> adapters ──> ports/domain
```

- `domain/` has no imports from Textual, HTTPX, SQLite, filesystem, or providers.
- `application/` depends only on `domain`, `ports`, and `contracts`.
- `interfaces/` never imports concrete adapters.
- `bootstrap.py` (future) is the only composition root.

## Single execution channel

```text
ModelResult
  → Tool Registry
  → Schema Validation
  → Workspace Facts
  → Deterministic Policy
  → Exact Approval (when required)
  → Execution Ticket
  → Executor
  → ToolResult + Evidence + Trace
```

The model never writes files or runs commands directly.

## State machine (high level)

```text
CREATED → RUNNING_MODEL → VALIDATING_TOOL
  → ALLOW → EXECUTING_TOOL → RUNNING_MODEL
  → ASK → WAITING_APPROVAL → EXECUTING_TOOL | RUNNING_MODEL
  → DENY → RUNNING_MODEL
→ VERIFYING → SUCCEEDED | STOPPED
Any active state → CANCELLED | STOPPED(budget)
Ambiguous effect → EFFECT_UNKNOWN → reconcile or ABANDONED
```

See `Haven_TUI_Coding_Agent_项目计划.md` for the full plan, budgets, eval matrix, and milestone checklist.
