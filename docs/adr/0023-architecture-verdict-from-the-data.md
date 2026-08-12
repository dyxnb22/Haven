# ADR 0023: the architecture verdict, decided from the data

Date: 2026-08-13
Status: Accepted (roadmap v3 phases 3 and 6)

## Context

Since ADR 0007 and ADR 0016, Haven has deferred five larger architectures —
a planner, a goal FSM, subagents, MCP, and a full (write-capable) LSP —
behind a benefit gate: build one only when measured failure data shows a
failure class it would fix, not on the assumption that more machinery helps.
Roadmap v3 was designed to produce that data (four real-task tiers, a
distribution, a head-to-head against a peer, a compaction A/B) and then make
the call. This ADR is the call. Roadmap v3 Phase 3 (a read-only LSP) carried
an explicit numeric gate; this ADR records its verdict and the broader one.

## The failure corpus

Every non-passing live run across tiers 1–5, the N=5 distribution, and the
opencode head-to-head, attributed:

- **Budget-tail non-convergence (most failures).** The model does not stop in
  time: the honesty tasks (hunting a bug that is not there) and the issue-
  style/hard-localization cases that thrash to the 24-step ceiling. The N=5
  distribution shows these as a *rate*, not a wall — the same task passes when
  it converges by ~19 steps. Root cause: the model's own judgement about when
  to stop, bounded (correctly) by the step budget.
- **Oracle-gaming (opencode, 2 cases; Haven historically, tier 1).** Editing
  the test to make the suite pass. Haven prevents this at execution via the
  scope-bound policy, not with more planning.
- **Semantic-localization ceiling (≈1 case: jinja idtracking).** One genuine
  "could not find/fix the subtle spot within budget."
- **Infrastructure / harness (the rest, all fixed).** Unretried drops,
  sandbox-oracle mismatch, in-tree pytest rootdir — none model-architectural.

## Decision

**Read-only LSP (Phase 3 gate): do not build — gate not met.** The gate was
≥5 failures attributable to semantic-localization limits; the corpus has
**≈1**. Grep/read localization is not the bottleneck the data shows; the
bottleneck is convergence/stopping. Building an LSP now would be adding a
capability against an unproven need — exactly what the gate exists to
prevent. Revisit if a future tier (larger repos, deeper call graphs) pushes
semantic-localization failures past the threshold; the tier-4/5 attribution
format already isolates them.

**Planner / goal FSM: do not build.** The dominant failure is the model not
*stopping*, not the harness failing to *plan*. A planner does not fix "hunts
too long"; the step budget already bounds it, and the `task.plan` tool plus
the loop already give lightweight structure. A planner would add state to
maintain and recover with no failure class pointing at it.

**Subagents: do not build.** No failure was a single agent overrunning a long
horizon for a reason a sub-delegation would fix; the long runs are
convergence tails on *single* localized tasks, which more agents would not
shorten. ADR 0007's benefit gate stays unmet.

**MCP: unchanged (ADR 0007/0016 deny-by-default).** Nothing in the corpus
needs a runtime-discovered tool; the cost (schema drift, injection surface)
still buys no measured win.

**What the data *does* endorse.** The one intervention the corpus justifies
is better *convergence/stopping*, and it is a prompt/behaviour concern, not an
architecture: the honesty-task data suggests an explicit "if you cannot
reproduce the symptom after a bounded search, stop and say so" is worth an
eval-backed system-prompt experiment (measured like the anti-gaming guardrail
was), not a new subsystem. That is left as the next small, reversible step —
not taken here without its own before/after measurement.

## Consequences

- Haven ships no new agent architecture. The verdict is the deliverable: five
  deferred systems remain deferred, now with the evidence that they are not
  the bottleneck rather than an assumption.
- The measurement apparatus (tiers, distribution, head-to-head, A/B) is the
  durable asset; it will re-decide this as the difficulty scales, in either
  direction.

## Rollback

None to roll back — this ADR builds nothing. If new data meets a gate, the
corresponding system is designed then, and this ADR is superseded with the
numbers that changed the call.
