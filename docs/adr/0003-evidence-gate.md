# ADR 0003: Evidence Gate for Success

## Status

Accepted

## Context

A model will happily say "Done! I fixed it." whether or not it did. If the
program accepts that as success, the agent's success rate is a measure of the
model's confidence, not of what actually happened to the repository. For a coding
agent, "success" must mean the change exists and passes verification.

## Decision

The program — not the model — decides whether a run succeeded, via a pure
`evaluate_evidence_gate(ledger)` over an ordered evidence ledger:

- A run that made **no** file edits may succeed on a final answer alone.
- A run that **did** edit files may only succeed if, recorded *after the last
  write*, there is both a `repo.diff` and at least one passing `repo.check`.
- A failing check, a missing diff, or a missing check blocks success.

When the model claims completion without sufficient evidence, the loop nudges it
(bounded to a couple of retries) with the specific gate failure, then stops with
`evidence_missing` rather than reporting a false success. Evidence entries carry
the journal sequence number, so a check that predates the last edit does not count.

## Consequences

- Success is defined by artifacts (diff + passing verification), and the final
  status can disagree with the model's prose.
- Requires an evidence ledger threaded through the tool pipeline and checkpoints.
- Read-only / question-answering runs are unaffected.

## Alternatives considered

- **Trust the model's final message**: rejected — unfalsifiable and gameable.
- **Always require a check even with no edits**: rejected — punishes legitimate
  read-only tasks and inflates cost.
- **A single boolean "tests passed" flag**: rejected — ordering matters; a stale
  pre-edit pass must not count, which is why evidence is sequence-stamped.
