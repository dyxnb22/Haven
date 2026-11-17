# Task handoff

**English** | [中文](HANDOFF.zh-CN.md)

The handoff page shows two parts separately: the program facts from the
checkpoint, and working notes the user maintains explicitly. Facts cover the
recorded modified paths, the diff count, and the current state of recent and
required checks. Notes cover what is done, decisions and their reasons, what was
tried, next steps, and what still needs checking. "Passed" or "authorized"
inside a note is always just text: it never affects permission, approval, or the
definition of success. Terminology is in
[project language](../CONTEXT.md); the persistence decision is in
[ADR 0048](adr/0048-explicit-task-handoff.md).

## TUI

Use these on an existing task:

```text
/handoff
/handoff prepare
/handoff edit
/handoff history
/handoff clear
```

`/handoff` shows the current facts, notes, source run, journal sequence, save
time, and any existing plan. `prepare` explicitly copies the titles of the
existing plan's completed and unfinished steps as a draft; it calls no model,
analyses no conversation history, and does not prove those steps are done. It is
refused when a handoff already exists, so handwritten content is never
overwritten — use `edit` directly instead.

The edit page offers five text fields, each capped at 600 characters and 2,000
in total. Write a reason straight into `decisions` when one is needed, e.g.
"keep the original API because existing callers depend on it". Limits are
validated before saving, and the current form content is kept on failure or a
version conflict. An edit binds the journal sequence from when the form was
opened; if another operation has advanced the journal, review the latest record
before saving.

Only a paused or finished user task can be edited. While the model is executing
you can view stored content but not edit it. While waiting on a question, use
**Keep paused** to retain the question and return to the main screen, maintain
the handoff, then reopen the form with `/answer`; Esc still means cancel the
task. Maintaining notes consumes no answer and completes no review.

`history` shows bounded text for the run's last 10 revisions, and `clear`
appends a clearing record. Clearing affects only the notes this task adopts
afterwards; older revisions and other branches' snapshots stay traceable.

## CLI

The following commands touch local storage only — no model service is created
and no project command is run:

```bash
haven handoff RUN_ID
haven handoff RUN_ID --prepare
haven handoff RUN_ID --export /tmp/handoff-notes.json
# Edit the five exported fields, using the Journal sequence shown at last view:
haven handoff RUN_ID --set /tmp/handoff-notes.json --expected-seq 42
haven handoff RUN_ID --history
haven handoff RUN_ID --clear
```

Export contains only the editable note fields and refuses to overwrite an
existing file. `--set` requires `--expected-seq`; prepare and clear may also
take it explicitly. The operations are mutually exclusive, the JSON file is
capped at 16 KiB, and unknown fields or notes over the limit are refused.
An over-long history entry shows an excerpt; full revisions are available from
`haven export RUN_ID --format jsonl`. Screen content strips terminal control
characters, and the TUI additionally escapes Rich markup.

## Compaction, recovery, and continuation

Handoff notes are stored independently of the conversation that gets compacted.
The edit window is frozen before the actual model run, which adopts the task's
latest explicit revision; the next checkpoint saves the snapshot actually used.
The view page still recomputes program facts from the newest checkpoint and
never infers state from notes. Editing notes after a pause does not rewrite the
original checkpoint, user answers, verification scope, or execution records.

Context places the notes in a separate **untrusted** fragment and the current
ledger facts in another **trusted** one, so compacting old tool output does not
lose the notes. Handoff reserves space ahead of optional skill bodies: notes are
capped at 3,000 characters and facts at 1,600, and a fixed header squeezing the
budget may excerpt or omit them, with excerpts marked. Native-prefix
continuation turns, like plans, append no tail for now; the next normal turn
restores it, and the full notes remain viewable from the handoff page or an
export.

Continue, plan implementation, and branches inherit a copy of the notes and
their source, but not the check success the notes claim. A new turn's ledger
still starts empty; after a clear, later turns no longer inherit, and a wholly
new task does not load them automatically. Legacy checkpoints have no handoff by
default. Revisions are appended to the event journal, so no database schema
upgrade is needed; older strict readers may reject new event or checkpoint
fields.

This version supports explicit tidying and revision only — no automatic model
summary is generated in the background. If automatic tidying is added later, its
trust boundary, storage, and actual benefit need separate validation.
