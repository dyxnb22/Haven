# ADR 0004: Durable Execution and Recovery

## Status

Accepted

## Context

Runs can be interrupted by Ctrl-C, a crash, or a kill mid-write. On restart we
must know what actually happened to the repository. A checkpoint alone is not
enough: the process may have died *between* performing a side effect and recording
that it did. Blindly replaying could double-apply an edit or re-run a command.

## Decision

Two complementary records, plus a conservative classifier:

- **Checkpoint** (`checkpoints`): a versioned, checksummed snapshot of run state
  (goal, status, budget/usage, transcript, evidence, files read, and artifact
  digests of pre-run file originals) for fast resume.
- **Event journal** (`events`): the append-only, per-event-digested authority for
  audit and replay. The **execution journal** (`executions`) records each side
  effect's lifecycle: `started → confirmed | failed | effect_unknown`.

On resume, `RecoveryService` classifies each started-but-unconfirmed effect by
comparing the file's current digest to the recorded preimage/postimage:

- current == preimage → **not run** (auto-reconciled, safe to resume);
- current == postimage → **confirmed** (auto-reconciled);
- neither → **effect unknown** → blocks resume.

Ambiguous effects are **never** auto-replayed; the user must reconcile explicitly
(`confirmed` / `not_run` / `abandon`). Checkpoints are checksum- and
schema-verified on load (fail closed), and workspace identity is re-checked.
`ReplayService` re-delivers journal events to a sink and calls neither the model
nor any tool.

## Consequences

- Interruptions are recoverable without duplicating side effects.
- Some crashes require a human decision; that is the safe default, not a bug.
- Recovery needs preimage/postimage digests and run-scoped file originals, adding
  bookkeeping to the tool pipeline and checkpoint.

## Alternatives considered

- **Checkpoint only, auto-replay the last step**: rejected — can double-apply an
  edit whose confirmation was lost in the crash.
- **Journal only, rebuild state by folding events every resume**: viable but
  slower and more code; a checksummed checkpoint plus the journal-as-authority is
  simpler and still auditable.
- **Treat any unconfirmed effect as "not run"**: rejected — unsafe when the effect
  may in fact have happened.
