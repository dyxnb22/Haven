# Module 07 — Durable execution: checkpoint, journal, recovery, replay

> Files: `src/haven/adapters/sqlite_session.py`,
> `src/haven/application/recovery_service.py`,
> `src/haven/application/replay_service.py`,
> `src/haven/contracts/checkpoint.py`, `src/haven/config.py`
> Tests: `tests/recovery/`, `tests/contract/test_session_store.py`
> ADR: [0004 — durable execution and recovery](../docs/adr/0004-durable-execution-and-recovery.md)

## Learning objectives

- Separate a fast-resume **checkpoint** from an append-only **journal**, and know
  which is the authority.
- Classify an interrupted side effect and **never auto-replay** an ambiguous one.
- Replay a run as a pure projection of the journal.
- Layer config so a project can only *tighten* limits.

## Checkpoint vs. journal

A checkpoint alone is not enough, because a process can die *between* performing
a side effect and recording that it did. Haven keeps both:

- **Checkpoint** (`checkpoints` table, `contracts/checkpoint.py`): a versioned,
  checksummed snapshot of run state (goal, status, budget/usage, transcript,
  evidence, files read, plan, and artifact digests of pre-run file originals).
  It exists for fast resume and is verified on load — a checksum or schema
  mismatch fails closed.
- **Journal** (`events` table): the append-only, per-event-digested authority for
  audit and replay. The **execution journal** (`executions` table) records each
  side effect's lifecycle: `started → confirmed | failed | effect_unknown`.

When the two disagree, the journal wins. The checkpoint is a convenience; the
journal is the truth.

## The rule that matters: never auto-replay an ambiguous effect

Read `RecoveryService.inspect`. On resume it classifies each side effect that was
`started` but never `confirmed`, by comparing the file's current digest to the
recorded preimage and postimage:

- current == preimage → **not run** (safe; auto-reconciled, resume can continue);
- current == postimage → **confirmed** (auto-reconciled);
- neither → **effect unknown** → resume is **blocked**.

An unknown effect requires an explicit human decision
(`haven reconcile RUN_ID CALL_ID --as confirmed|not_run|abandon`). Haven never
guesses, because re-running a possibly-completed write is worse than asking. This
is the single most important sentence in the module:

> When you cannot prove a side effect did not happen, do not repeat it.

`tests/recovery/` covers not-run, confirmed, ambiguous, abandoned, and
workspace-identity-mismatch. Recovery also re-checks workspace identity before
resuming — a checkpoint from a different repo is refused.

## Replay is a projection, not a re-run

`ReplayService` re-delivers the journal's events to a sink and calls neither the
model nor any tool. Because the TUI is a pure reducer over the same event stream
(Module 08), replay reconstructs an equivalent screen. This is why the golden
trace test can assert that TUI and headless produce identical traces: they are
both just consumers of the journal.

## Persistence details worth copying

Read `sqlite_session.py`:

- WAL mode; everything lives in the platform data dir (`HAVEN_DATA_DIR` overrides
  it), always **outside** any workspace so `repo.*` tools can never reach the
  database.
- `(run_id, seq)` is unique; each event stores a content digest that is verified
  on load.
- **Approval consumption is a conditional `UPDATE`** — this is where Module 04's
  single-use guarantee physically lives: the update matches on id *and* digest
  *and* `consumed_at IS NULL`, so it can win at most once.
- The in-memory store (`memory_session.py`) implements the same port, and
  `tests/contract/test_session_store.py` runs the *same* contract tests against
  both, so the fast test double cannot drift from the real one.

## Config that can only tighten

`config.py` merges built-in safe defaults → user config → provider environment
variables and the CLI-selected budget tier → project `.haven.toml`. The project
file is applied last but may only *lower* budgets and *register* recipes; it can
never raise a limit, change the provider, or widen the agent's policy. A recipe
can declare fixed process capabilities such as network/readable roots because it
is user-authored authority, not model input. Secrets come only from environment
variables and are reported present/missing, never printed.

## Exercises

1. **Simulate a crash.** Follow `tests/recovery/` to build a run whose edit is
   `started` but not `confirmed`. Run the three cases: file matches preimage
   (resume), matches postimage (resume), matches neither (blocked).
2. **Break a checkpoint.** Corrupt a checkpoint checksum in a test and assert the
   loader fails closed.
3. **Replay.** Run a scripted journey, then `haven replay RUN_ID`, and confirm no
   model or tool is invoked (the run is already finished; nothing new happens).
4. **Tighten-only.** Write a `.haven.toml` that tries to *raise* `max_steps`
   above the default and confirm via `haven config explain` that it did not take
   effect.

## Self-check

- What does each of checkpoint and journal solve, and which is authoritative?
- State the ambiguous-effect rule in one sentence.
- Where does the single-use approval guarantee physically live, and why is a
  conditional `UPDATE` the right primitive?

## Further reading

- ADR 0004 for the recovery design.
- Commits `8e91e69` (persistence), `7ff184e` (recovery + replay),
  `d55ff10` (config).
