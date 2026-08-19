# ADR 0030: effect attribution must preserve identity, existence, and uncertainty

Date: 2026-08-20
Status: Accepted (tightens ADR 0004, 0012, 0019, 0020, and 0029)

## Context

The final release audit found several places where distinct facts collapsed to
the same representation: provider call IDs were globally unique only by
assumption; an absent path and an existing empty file both became `""`; a
cancelled process could leave an unknown effect while the run said only
`cancelled`; and recipe authority was fixed in config but not fully repeated on
the approval card. These are not independent cosmetic defects. Each lets the
journal, diff, recovery classifier, or human describe less authority than the
system actually exercised.

## Decision

Haven treats exact effect attribution as one cross-layer invariant:

- execution rows are identified by `(run_id, call_id)`; SQLite schema v3
  migrates existing rows without discarding them;
- run baselines use `None` for an absent path and `""` for an existing empty
  file, including checkpoint and rewind compatibility for older records;
- new-file publication is no-clobber and atomic; an approval-time absence check
  is not authority to overwrite a path that appears before commit;
- once an effect starts, cancellation, unverifiable postimages, or failed
  compensation finish as `EFFECT_UNKNOWN`, never as a clean failure/cancel;
- process completion covers the whole process group, including background
  descendants that inherit output pipes;
- process scratch roots are atomically created outside the untrusted workspace,
  scoped to one `repo.exec` run or recipe invocation, and never reused by path;
- a check approval repeats its effective authority: command, writable
  workspace, network flag, and additional readable roots;
- content-addressed artifacts are validated by digest on both write and read.

## Consequences

Recovery can classify two runs that reused the same provider call ID, empty-file
creation/deletion, and interrupted processes without guessing. Some operations
that previously looked like ordinary failures now stop the run for manual
reconciliation. Check cards are longer because consent is based on runtime
facts, not merely on where configuration came from. The workspace lease remains
local and advisory, but its stale-break check/write section is serialized with a
native file lock and ownership uses a random token, closing the former
same-machine winner race. Ignoring the historical workspace `.haven-scratch`
path also prevents a repository-controlled symlink from expanding a sandbox's
writable roots.

Regression coverage spans the session-store contract, workspace/create/patch
tests, recovery tests, process-executor tests, approval-flow tests, and the
end-to-end cancellation journey.
