# Architecture decision map

Start here instead of reading all ADRs in number order. The records remain
immutable history; this page identifies the decisions that actively constrain
changes and separates them from evaluation verdicts.

## Load-bearing guarantees

- Proposal and success boundaries: [0002](adr/0002-tool-execution-boundary.md),
  [0003](adr/0003-evidence-gate.md), and
  [0004](adr/0004-durable-execution-and-recovery.md), tightened by
  [0030](adr/0030-exact-effect-attribution.md).
- Process and workspace security: [0009](adr/0009-os-sandbox-and-general-exec.md),
  [0012](adr/0012-attributing-process-writes.md),
  [0013](adr/0013-sandbox-scope-exec-vs-check.md),
  [0017](adr/0017-exec-workspace-read-only.md),
  [0018](adr/0018-protected-path-tamper-fails-the-call.md), and
  [0026](adr/0026-exec-friction-follows-the-operands.md), with exact effect
  attribution in [0030](adr/0030-exact-effect-attribution.md).
- Extension and mutation contracts: [0016](adr/0016-extension-boundary.md),
  [0019](adr/0019-apply-patch-one-approval-one-transaction.md), and
  [0029](adr/0029-recipe-declared-toolchain-roots.md).
- Durable runtime behavior: [0015](adr/0015-sessions-and-multi-turn.md),
  [0020](adr/0020-session-runtime-steering-rewind-lease.md),
  [0025](adr/0025-standing-approval-for-identical-checks.md), and
  [0027](adr/0027-provider-failures-that-are-recoverable.md).

## Supporting design choices

- Scope, offline evaluation, and budgets: [0001](adr/0001-language-and-scope.md),
  [0005](adr/0005-offline-eval-and-scripted-model.md), and
  [0006](adr/0006-long-horizon-planning-and-budgets.md).
- Context, caching, and cost: [0008](adr/0008-prompt-cache-prefix-stability.md),
  [0010](adr/0010-deterministic-compaction-and-budget-tiers.md),
  [0011](adr/0011-model-profiles-and-cache-aware-cost.md),
  [0014](adr/0014-deepseek-reasoning-replay.md), and
  [0022](adr/0022-native-prefix-continuation-and-token-calibration.md).
- Headless operation: [0021](adr/0021-headless-write-and-discovery-accept.md).

## Evaluation verdicts and deferrals

These explain why capabilities were not built; they are useful history, not
additional runtime contracts:

- [0007](adr/0007-subagents-mcp-and-deterministic-review.md): subagents, MCP,
  and model-driven review.
- [0023](adr/0023-architecture-verdict-from-the-data.md): planner, FSM, and LSP
  verdicts from the measured corpus.
- [0024](adr/0024-compaction-boundary-and-summary-tier-gate.md): summary-tier
  deferral and its reopen conditions.
- [0028](adr/0028-the-daily-use-capability-gaps.md): background tasks, web, and
  image-input deferrals.

## Admission rule for new ADRs

Use an ADR only when a decision changes at least one of these and is costly to
reverse:

- a security or trust boundary;
- a cross-layer dependency rule or public tool/API surface;
- a persisted-state or wire compatibility contract;
- the definition of a successful run or a recovery invariant.

Use a [decision note](notes/) for experiments, deferrals, local conventions,
feature comparisons, and designs that are not being built. A new ADR should
normally contain only context, decision, and consequences; stay under roughly
80 lines; make one decision; and link to `EVAL_LIVE.md` or another report
instead of embedding the measurement narrative. Historical ADRs are corrected
by forward annotations and backlinks, never silently rewritten.
