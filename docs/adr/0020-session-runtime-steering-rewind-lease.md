# ADR 0020: session runtime — queued steering, rewind, writer lease

Date: 2026-08-12
Status: Accepted (builds on ADR 0015)

> **Forward annotation (2026-08-20):** ADR 0030 keeps the local advisory scope
> but replaces read-back-only contention with a native file-lock guard and a
> random ownership token. Its exact-effect rule also makes cancellation during
> an in-flight process finish as `EFFECT_UNKNOWN`.

## Context

ADR 0015 made a session a chain of runs: follow-ups inherit the transcript,
each turn keeps its own budget and ledger. Three gaps remained, named by all
three external audits: input during a run was refused outright (cancel first),
undoing a finished run required manual file surgery, and nothing stopped two
Haven processes from interleaving writes on one workspace between another
run's approval and its execution.

## Decision

**Steering: queue at arrival, deliver at the turn boundary.** `RunService.
steer(text)` accepts input while a run is active, journals it immediately as a
`steer.queued` event (durable — an interrupted run's undelivered steering is
in the record), and the loop drains the queue at the top of each iteration,
turning each item into an ordinary user message before the next model request.
Nothing in flight is interrupted: the model stream and the tool channel are
untouched mid-turn, which is the property that keeps approval binding and
effect journaling exactly as they were. Steering queued on a run's final turn
has no later boundary and is deliberately dropped (still journaled), never
leaked into a later run. The TUI queues instead of refusing.

**Rewind: fail-closed compensation, not replay.** `RecoveryService.rewind`
restores every file a finished run changed to its pre-run content — the
run-scoped originals already archived for the diff — but only where the disk
still matches the run's own final postimage for that path. A file changed
since (by the user or a later run) blocks the rewind rather than being
clobbered, the same stance reconcile takes on ambiguous effects. Files the
run created (first edit evidence with an empty preimage) are removed rather
than emptied. The journal is never rewritten; rewind is a new action on the
record, not an erasure of it. Exposed as `haven rewind RUN_ID`.

**Fork: already structural, now pinned.** `continue_run` accepts *any*
checkpointed run id, so branching from an older turn is a follow-up whose
`parent_run_id` records the branch point; a test pins that the fork's
transcript carries its ancestor's content and not its sibling's.

**Writer lease: one writable process per workspace.** An advisory
single-writer lease (JSON file under the user data dir, keyed by resolved
workspace path) is taken by any non-read-only process at bootstrap. A live
holder (probed pid on the same host, fresh heartbeat otherwise) causes the
contender to *downgrade to read-only* with an explicit warning — refusing
outright would punish the common accidental second window. Stale leases
(dead pid, or a heartbeat older than 15 minutes) are broken and taken over,
with a read-back confirmation so two simultaneous breakers cannot both
believe they won. Scope is one machine and advisory by design; it is not a
distributed lock.

## Consequences

- The user can redirect a running agent without killing its turn; the cost
  is that delivery waits for the current turn to finish, which is the price
  of never interrupting an effect.
- Rewind's safety rule means a run whose files were later touched cannot be
  rewound wholesale; per-file partial rewind is deliberately not offered
  (partial undo of one logical change is a new inconsistent state).
- A crashed process leaves a lease that the next process breaks after the
  pid check — no manual cleanup. A hung-but-alive process on the same host
  holds the lease until it exits (a live probed pid is authoritative; the
  15-minute heartbeat staleness applies only to holders that cannot be
  probed, i.e. another host or another user's pid).
- Regression coverage: `tests/integration/test_session_runtime.py`
  (steering boundaries, leakage, rewind both ways, fork), and
  `tests/unit/test_workspace_lease.py` (contention, staleness, takeover,
  foreign-holder release safety).

## Rollback

Each piece is independent: remove the TUI queue call to restore
refuse-while-running; drop the lease acquisition in bootstrap; `rewind` is
additive CLI surface.
