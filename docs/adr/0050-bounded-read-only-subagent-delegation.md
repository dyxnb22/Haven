# ADR 0050: Bounded read-only subagent delegation

Date: 2026-11-16
Status: Accepted

## Context

ADR 0016 deferred the multi-agent extension behind a measurable trigger, and
ADR 0007 deferred a model-driven Reviewer subagent because its acceptance gate
— defect-detection rate and false positives against the single-agent baseline —
cannot be measured offline: with `ScriptedModel` those numbers are authored, not
measured.

What shipped is neither of those. `agent.delegate` gives the parent one level of
child model loops that may only *read*, and the parent blocks while a child runs.
It is an investigation capability that moves file content out of the parent's
transcript; it is not a second opinion on whether the run succeeded.

## Decision

Adopt `agent.delegate` as a compiled-in, read-only tool.

- **Classification.** It joins `READ_ONLY_TOOLS` in `domain/policy.py`, so it is
  enumerable with the rest of the tool set and can never be auto-allowed as a
  side effect.
- **Depth is structurally one.** The child's context carries `parent_run_id`,
  and `ToolPipeline` admits a run that has one only the tools named in
  `CHILD_READ_TOOLS`. `agent.delegate` is not in that set, so a child cannot
  delegate — not by a policy judgement, but because the allowlist never names
  it. The child's advertised tool list is filtered the same way.
- **Budget is the parent's.** Child step and tool-call limits are clamped to the
  parent's remaining budget with one parent turn reserved; wall time, tokens and
  cost are clamped to what is left. The child's steps, tool calls, tokens, cost
  and estimation flag are charged back on every exit path. A child can exhaust
  the run; it can never extend it.
- **Scope is checked, not asserted.** `file_scope` entries are resolved through
  `path_facts`; a path outside the workspace or protected is denied.
- **Inheritance is deliberately partial.** A child inherits the parent's
  guidance, project notes, verification requirement and already-selected skills,
  and shares its provider and workspace. It does not inherit the transcript, and
  `skill.read` is outside `CHILD_READ_TOOLS`, so it cannot load further skill
  content. No process, no file mutation, no imported MCP resource.
- **Success is unchanged.** Returned findings are model judgements about code.
  The deterministic review of added lines in `domain/review.py` is still what
  gates a run that edited files, so ADR 0007's Option B remains deferred — this
  does not replace it.

## Consequences

The parent's transcript carries a child's findings instead of the files it read,
which is the only claimed benefit: bounded context, same evidence. Child runs
are persisted and emit their own lifecycle events, so a parent trace shows what
was delegated and what came back, and a child that exhausts its budget returns
an incomplete result rather than a confident wrong one. Delegation is
synchronous, so it costs wall time and buys nothing when the answer is a single
`repo.read`. Parallel investigation would be a separate decision; this one does
not assume it.

See [remaining capabilities](../REMAINING_CAPABILITIES.md) for the child
contract and [ADR 0016](0016-extension-boundary.md) for the invariants above.
