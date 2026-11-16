# ADR 0047: Retained process output as observations

Date: 2026-11-07
Status: Accepted

## Context

Tool completion previously exposed only a short summary; the executor retained
64 KiB tails. Early diagnostics could disappear, and neither the user interface
nor a resumed session could recover the fuller output. Putting entire logs in
events or checkpoints would inflate model context and persisted hot state.

## Decision

Capture at most 2 MiB per stream, retaining head and tail with explicit omission
markers above the limit. Keep the existing observation-tail contract. Extract
bounded diagnostics from the fuller capture, including current Ruff output.
On timeout, retain already-read output after terminating the process group.

Store a decoded text log in the existing content-addressed artifact store.
`tool.completed` gains optional `process` metadata: exit/timeout, workspace,
recipe ID when applicable, digest, capture truncation and bounded diagnostic
text. Omit the field on non-process/legacy completions. Existing fields and
successful model payloads retain their meaning. New readers accept old events;
older strict readers may reject the additional field in new events.

Read-only TUI/CLI viewers resolve an artifact only through the selected run and
call, verify integrity, bound display pages and escape terminal control syntax.
Explicit export preserves retained log text and refuses file overwrite.
GC marks surviving completion-event references as well as checkpoint references;
artifact sweeping waits while runs are active to protect the write-before-event
window. Sweep candidates are snapshotted before runs, and logs include run/call
identity so output written by a later run cannot reuse an old sweep candidate.
Deleting a run releases its unique output artifacts.

## Consequences

Process reports remain observations. They do not create check evidence, relax
verification requirements, replay side effects or grant permissions. Recovery
still derives required-check states from its checkpoint ledger. Output capture
is finite; truncation is visible and readers never promise the omitted middle.
Unfinished/cancelled calls without completion events may have no retained log.
This extends [0040](0040-ast-guided-syntax-gate-and-diagnostic-feeder.md)
without changing [0012](0012-attributing-process-writes.md) or
[0046](0046-user-selected-verification.md).
