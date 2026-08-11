# ADR 0006: Long-horizon planning and budget defaults

## Status

Accepted — benefit gate passed for both parts.

## Gate: problem

Two related limits showed up once `repo.create` and scoped edits made real
multi-file tasks possible:

1. **Budgets are too tight to survive a single retry.** The default was 12 steps
   and 24 tool calls. Counting the minimum viable trajectory: read (1), edit (2),
   diff (3), check (4), answer (5). Add a created file (6), modest exploration —
   list, search, two reads (9) — and one failed check with a fix, re-diff, and
   re-check (12). A run therefore hits the step budget at exactly the moment it
   would have recovered from its first failure, and reports
   `step_budget_exhausted` rather than the real outcome.

2. **A plan cannot survive context truncation.** Anything the agent says about
   its intent lives in the transcript, and `ContextBuilder._fit_to_budget`
   deliberately drops the oldest tool outputs first. On a long task the model's
   own stated plan is exactly the sort of early message that decays, so the
   agent loses the thread precisely when the task is long enough to need one.

## Gate: current baseline

- Measured: the 7 offline task cases complete in 3–6 steps each — but those are
  scripted, optimal trajectories with zero exploration and zero retries. They
  establish the floor, not the realistic cost.
- No structured notion of "what am I doing and how far along am I" exists in
  `RunContext`, in the trace, or in the TUI.
- Stuck-loop detection and the Evidence Gate already prevent thrash and false
  success, so raising budgets does not remove a safety net; it removes a
  premature stop.

## Gate: options considered

**Budgets**

- *Keep 12/24.* Rejected: it truncates recoverable runs, and the failure mode is
  indistinguishable from a genuinely stuck agent in the report.
- *Make budgets adaptive (extend when evidence is improving).* Rejected: an agent
  that can extend its own budget has no budget. The value of a hard limit is that
  it is not negotiable.
- **Raise to 24 steps / 48 tool calls.** Accepted: covers exploration plus about
  three fix-verify rounds, stays a hard ceiling, and users can still lower it via
  config (project files may only tighten).

**Planning**

- *Do nothing; rely on the transcript.* Rejected: the truncation problem above.
- *Summarize the transcript with the model when it grows.* Rejected explicitly in
  ADR 0001's spirit and in the plan: a model-written summary can invent facts
  that later turns treat as established, including permission-shaped facts.
- **A `task.plan` tool whose result lives in run State, is re-rendered into
  Context every turn, and is checkpointed.** Accepted.

## Decision

1. Default budgets become **24 steps / 48 tool calls**. Wall clock, token, and
   cost ceilings are unchanged.
2. Add `task.plan`: the agent submits an ordered list of short steps, each
   `pending | in_progress | done`. The tool has no effect outside the run's own
   state, so it is a new `STATE_TOOLS` category that is `allow` in both permission
   modes — it can be called in `read_only` mode too, where it is the only way to
   express intent.

The plan is stored in `RunContext` (State), re-rendered by `ContextBuilder` into
every subsequent request (Context), emitted as a `plan.updated` event (Trace),
and persisted in `CheckpointV1` so it survives resume. This is the State ≠
Context ≠ Trace separation doing real work: the plan cannot be truncated away,
because it is not a transcript message — it is regenerated from state each turn.

The rendered plan is labelled **untrusted**, because its text was authored by the
model. Labelling it trusted would repeat the AGENTS.md mistake recorded in
`docs/POSTMORTEM.md`.

## Gate: metrics and how we will know

- Offline (available now): the plan appears in `context.built` for every turn
  after it is set, including turns where older tool output was truncated; it
  survives a checkpoint/resume round trip. Both are asserted by tests.
- Not measurable offline: whether planning improves real task success. A scripted
  model's plan is scripted. That number can only come from `haven eval --live`
  and is deliberately not claimed here.

## Gate: risks

- **Plan theater.** The model can write a plan and ignore it. Mitigated by
  keeping the plan advisory and out of the success criteria: the Evidence Gate
  still decides success from diff and checks, never from plan status.
- **Context cost.** The plan is re-sent every turn. Capped at 12 steps of 120
  characters, so the worst case is roughly 1.5 KB per request.
- **Budget raise hides regressions.** Mitigated by stuck-loop detection, which
  fires on repetition regardless of remaining budget, and by the budget eval case
  that pins the exhaustion behavior with an explicit low budget.

## Rollback

Both parts are independent and cheap to revert: restore the two `Budget`
defaults, and remove `task.plan` from `ARGS_MODELS` / `STATE_TOOLS` (the policy
completeness test will then fail loudly until the plan field is removed from
`RunContext` and `CheckpointV1` as well).
