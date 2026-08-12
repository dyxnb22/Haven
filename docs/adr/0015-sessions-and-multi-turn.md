# ADR 0015: Follow-up turns that inherit the conversation

## Status

Accepted — the core of the largest UX gap the external reviews named. This ADR
delivers multi-turn continuity and records what is deliberately left for later.

## Gate: problem

Haven ran one goal and stopped. Asking a follow-up started a fresh
`RunContext` with an empty transcript, and the TUI refused input while a run was
active. All three reviews (against Reasonix, Codex CLI, and opencode)
independently called this the biggest usability gap: Haven was a bounded task
runner with a TUI, not a coding session you can steer and build on.

## Gate: options

- *Make `RunContext` mutable across turns and reuse one long-lived run.*
  Rejected: it would blur the durable-run boundary that recovery, checkpointing,
  and the "one run, one stop reason" invariant all depend on.
- **Keep each turn a distinct Run, but seed a follow-up's transcript from the
  prior run's checkpoint.** Accepted. Continuity comes from the carried
  transcript; durability semantics are untouched.

## Decision

`RunService.continue_run(previous_run_id, follow_up)`:

- Loads the previous run's checkpoint (refuses if absent).
- Builds a new Run — new id, fresh budget, fresh evidence ledger — whose
  transcript is the prior transcript plus a `user` message carrying the
  follow-up. The plan and `files_read` carry forward; the ledger does not, so
  the follow-up's success is judged on its own edits.
- Keeps the session's head goal stable (the first goal), so the follow-up is
  threaded in as a transcript message rather than rewriting the cacheable prefix
  (ADR 0008). The compaction from ADR 0010 is what makes a growing session
  transcript sustainable, which is why that work came first.
- Records lineage on the `RunCreated` event as `parent_run_id`.

Entry points:

- Headless: `haven continue <run_id> <follow_up>` (read-only, like `haven run`).
- TUI: a prompt submitted after a finished run continues it instead of starting
  a blank run; the first submit of a session still starts fresh.

## What this does and does not change

- **Unchanged:** every turn is still a durable Run with its own checkpoint,
  journal, budget, and single stop reason. Recovery is unaffected.
- **Unchanged:** the Evidence Gate, trust labelling, and the execution channel.
- **Changed:** a follow-up sees the prior turn's work, in both the TUI and
  headless.

## Deliberately deferred (with reasons)

These are real parts of a "session" and are left for a later pass rather than
half-built:

- **Live steering / queued input** while a run is active. The TUI still asks the
  user to cancel first. Delivering input at a turn boundary safely is a
  concurrency change to the run loop, not a data change, and it deserves its own
  design.
- **Rewind and fork as user actions.** The data foundation exists — every run
  has checkpoints and lineage — but exposing "branch from turn N" is a UX
  surface (a picker, a visible tree) that should be designed deliberately, not
  bolted on.
- **Visual chat continuity in the TUI.** A follow-up currently still resets the
  diff and evidence panels (they are per-run and a fresh ledger is correct);
  making the chat panel visibly continuous across turns is polish, not the
  functional fix, which is that the model now has the context.

*Amended 2026-08-12:* two lifecycle guards external review showed were missing
are now in place. `continue_run` refuses a workspace whose digest differs from
the checkpoint's (a follow-up must not graft a transcript onto a different
repository — recovery already made this check), and it resets the workspace's
run-scoped diff originals, so a second turn's `repo.diff` reports only its own
changes instead of leaking the first turn's edits through the long-lived TUI
workspace.

## Gate: metrics

- `tests/integration/test_session_continuity.py`: a follow-up's request carries
  the first turn's answer and the new instruction; the follow-up gets a fresh
  budget; lineage is recorded; continuing a run with no checkpoint is refused;
  a different workspace is refused; a follow-up's diff excludes the prior
  turn's changes.
- `tests/tui/test_tui_journey.py`: a second TUI prompt produces a new run whose
  `parent_run_id` is the first, proving the wiring end to end.
- Golden trace regenerated for the additive `parent_run_id` field only.

## Rollback

Remove `continue_run`, the `haven continue` command, and the TUI follow-up
branch (submits always start a fresh run again). Drop `parent_run_id` from
`RunCreated`; persisted journals keep it harmlessly (it defaults to "").
