# Freeze user-selected verification requirements per run

The original evidence gate checks results that happened to run; it cannot prove
that all user-required checks ran. Make a named verification group an explicit
user selection, separate from tool permissions and model-generated plans.

Freeze its recipe IDs and canonical invocation digests before execution.
Digest the complete recipe definition and appended arguments. Only the
configured invocation without extra arguments can satisfy a required check.
Its latest result must pass after the last write, including when no files
were edited. Preserve the original diff, check and review rules for edits.

Store selection and actual command digests in optional checkpoint fields.
Resume validates the saved definitions against current configured recipes;
missing or changed definitions refuse execution before consuming an answer.
Group declaration changes cannot lower an existing selection. New follow-up
or implementation runs inherit it unless the user explicitly changes or clears
the new turn's selection. They receive new evidence ledgers.

Model tools cannot mutate this selection. In particular, `recipe.propose`
cannot overwrite a required ID: policy denies this independently of approval
mode, with an execution guard as defense in depth. Additional filtered checks
may run through normal approval but cannot masquerade as the required command.
Planning saves scope and defers checks until confirmed implementation.

No-group runs and old checkpoints keep their existing semantics. New optional
event fields are omitted when unused, preserving the existing no-group trace.
Selected groups add structured check results for live display and replay.
Older versions can reject the added persisted fields; no downgrade conversion
silently removes requirements.

This extends [0003](0003-evidence-gate.md),
[0004](0004-durable-execution-and-recovery.md) and
[0043](0043-ephemeral-reproducer-and-dynamic-verification-oracles.md).
