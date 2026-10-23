# ADR 0033: Tiered approval modes (ask, smart, full)

Date: 2026-10-22
Status: Accepted (amends ADR 0002 and ADR 0021; preserves ADR 0009, ADR 0025, and ADR 0032)

## Context

In production use, interactive agents face approval fatigue: requiring human confirmation for every single file edit (`repo.edit`) or check recipe (`repo.check`) trains users to blindly press approval keys without inspecting the diff preview, defeating the purpose of digest-bound approvals.

Commercial agents (such as Codex CLI and Claude Code) address this by providing tiered approval configurations:
1. Strict confirmation for all actions ("Ask for approval").
2. Intelligent risk-based auto-approval ("Approve for me" / only ask for high-risk actions).
3. Full autonomous access ("Full access").

Haven previously offered only two permission modes: `interactive` (where every mutation requires an approval card) and `read_only` (hard denial of all mutations). ADR 0021 introduced headless write modes with `--approval-policy reject | trusted-recipe | all`, and ADR 0025/0032 introduced byte-identical check reuse and skill-declared standing grants. However, in interactive TUI and daily usage, approval fatigue remained a primary source of friction.

## Decision

**Introduce three tiered approval modes (`ApprovalMode`: `ask`, `smart`, `full`) for resolving policy `ASK` outcomes, while keeping the deterministic policy layer, OS sandboxing, and Evidence Gate completely intact.**

### The Three Modes:
- **`ask` (Strict confirmation, default)**:
  Every mutation proposal that yields `PolicyDecision.ASK` suspends execution and displays an `ApprovalRequested` card for explicit human decision (`a`/`r`).
- **`smart` (Approve for me / Intelligent risk-based approval)**:
  Evaluates the policy outcome's `RiskLevel`. Low- and medium-risk operations (`repo.edit`, `repo.create`, `repo.check`) are automatically approved and ticketed without prompting the user. High-risk operations (`repo.delete`, `repo.move`, `repo.apply_patch`, and `shell_passthrough` commands) continue to halt and prompt the human for confirmation.
- **`full` (Autonomous within workspace)**:
  All valid operations that yield `PolicyDecision.ASK` within the workspace boundary are automatically approved and ticketed without human interruption.

### Six Invariants Preserved:
1. **`DENY` is absolute and cannot be converted to `ALLOW`**:
   The policy function `evaluate_policy(mode, facts)` remains a pure function. Outside-workspace escapes, touches to protected paths (`.git`, `.haven`), executions without an OS sandbox backend, and writes in `PermissionMode.READ_ONLY` always return `DENY`. `ApprovalMode` only resolves `ASK`; it is never consulted on `DENY`.
2. **Single execution channel**:
   Auto-approved operations under `smart` or `full` modes still mint an `approval_id`, record and decide the approval in SQLite, emit `Notice` and `ApprovalDecided` events to the journal, and enforce atomic consumption before executing. The event trace remains 100% auditable and replayable.
3. **Evidence Gate is never bypassed**:
   Runs modifying files cannot claim success without a real diff and a green verification check recorded after the last write. Autonomous execution does not relax verification standards.
4. **Policy-level high-risk classification**:
   Irreversible deletions (`repo.delete`), path mutations (`repo.move`), and multi-file transactions (`repo.apply_patch`) are classified as `RiskLevel.HIGH` in policy evaluation to guarantee they require human review under `smart` mode.
5. **Runtime configurability and TUI feedback**:
   Configurable via CLI (`--approval-mode ask|smart|full`), environment variable `HAVEN_APPROVAL_MODE`, and TUI runtime command `/mode [ask|smart|full]`. The active approval mode is displayed prominently in the TUI header (`interactive [smart]`).
6. **Unified with headless operation**:
   Extends and unifies the headless approval strategies from ADR 0021 into a consistent system-wide abstraction.

## Consequences

- Eliminates approval fatigue for repetitive, low-risk editing workflows while maintaining human gates on destructive or complex multi-file operations.
- Preserves complete auditability: replays reflect automated approval notices alongside manual decisions.
- Golden trace and test harnesses remain deterministic and offline-testable.
