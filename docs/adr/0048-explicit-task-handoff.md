# ADR 0048: Explicit task handoff notes

Date: 2026-11-07
Status: Accepted

## Context

Deterministic compaction preserves tool metadata, not decisions and failed
approaches written as prose. Long tasks need a reviewable working record without
turning a model's completion or permission claims into execution authority.

## Decision

Provide five bounded note fields maintained by explicit user action. Preparation
copies existing plan titles into an untrusted draft; it does not call a model.
Keep program facts derived from the checkpoint/current ledger separate from notes.
No tool can silently author or promote these notes to trusted evidence.

Revisions and clearing append `handoff.updated` events. Bind updates to the whole
reviewed journal sequence and latest checkpoint, allowing only paused/terminal
user runs. A conditional insert checks status and sequence atomically. Allocate
all SQLite event sequences inside the insert, replacing the connection-local
cache so external note edits cannot cause duplicate event sequences on resume.
No new table or database schema version is needed.

Freeze editing before a model drive, then adopt the latest explicit revision.
Checkpoint an optional handoff snapshot containing notes and their source run,
event, timestamp and originating checkpoint sequence. Continuations/implementation
inherit the snapshot as context; a new run's evidence ledger remains empty.
Clear overrides inherited content on that branch; old revisions remain audit data.

Inject bounded untrusted notes and separately generated ledger facts near the
context tail, outside transcript compaction. Reserve space before dropping
optional skill bodies; bounded excerpts can still be required by the hard budget.
The existing native-prefix continuation exception also omits this tail temporarily.

## Consequences

Editing cannot consume answers, change approvals, alter required checks or
manufacture verification evidence. Recovery still performs its original checks;
explicit note revisions can be adopted without changing the saved project-guidance
snapshot from [0044](0044-project-guidance-snapshots.md). Old checkpoints default
to no handoff; older strict readers may reject the additive fields/events.
Automatic model summarization remains deferred; this decision covers explicit
preparation, review, revision and persistence only.
