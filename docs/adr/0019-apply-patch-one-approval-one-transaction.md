# ADR 0019: repo.apply_patch — one approval, one transaction

Date: 2026-08-12
Status: Accepted

## Context

A multi-file change was N sequential `repo.edit` calls: N approval prompts, N
chances for a stale preimage, no single reviewable diff, and no atomicity — a
failure at edit 3 of 5 left a half-applied change. All three external audits
independently ranked a structured multi-file patch as the top capability gap,
and the tier-4 refactor evaluation cannot be built without it.

## Decision

One tool, `repo.apply_patch`, through the existing channel unchanged.

**Structured operations, not patch text.** A patch is an ordered tuple of
typed operations (edit hunk / create / delete / move) with a pydantic
discriminator — not a unified-diff string. Deterministic application needs
exact preimage binding; parsing model-generated diff text and fuzzily locating
hunks would reintroduce precisely the ambiguity `old_string`-uniqueness rules
exist to prevent. Context-tolerant ("fuzzy") matching is explicitly rejected.

**Simulation before approval.** The workspace simulates the whole patch in
memory: later operations see earlier effects, conflicts (edit-after-delete,
create-over-existing, move onto an occupied path) fail closed, the
read-before-edit rule applies per file (except files the patch itself
authored), and the plan reduces to *net per-file effects*. A move plans as a
provable delete plus a provable create, which eliminates the ambiguous
mid-move recovery window — each end carries its own proof.

**One approval, binding everything.** The approval digest covers the canonical
arguments plus an aggregate preimage digest over the sorted
`{path: digest}` map of every pre-existing file touched, and the preview shown
to the user is the entire unified diff. After the human decides, every pin is
re-verified against disk (the TOCTOU guard, widened from one file to the set),
and `apply_patch` re-verifies again before writing — any drifted file fails
the whole patch closed with nothing applied.

**Writes land before removals, with a journaled rollback.** All new contents
are staged as fsynced temp files first; commit renames writes into place and
unlinks removals last, so no crash point loses data. On a mid-commit failure
the compensation restores every already-committed file; if the compensation
itself fails, a distinct `PatchRollbackError` (deliberately not a
`WorkspaceError`) surfaces as an **unknown effect** — the run stops, recovery
blocks resume, and a human reconciles.

**Journaled as constituent effects.** The execution journal records one entry
per net file effect (`call_id#N`, shaped as repo.edit/create/delete with the
expected postimage from the plan), so the existing recovery classifier proves
each file independently after a crash. Evidence is one ledger entry per
changed file under a single envelope; the Evidence Gate semantics are
untouched — a patch still needs a green registered check after it.

## Consequences

- Approval count on multi-file work drops to one; the reviewer sees the whole
  intent as one diff instead of five opaque substitutions.
- `TOOL_VERSION` bumped to "4" (the ticket/approval digests incorporate it).
- The model is nudged (tool description) to prefer the patch whenever a change
  spans files; single-file tools remain for single-file work.
- Offline eval gains `task-apply-patch` (full journey) and
  `sec-patch-protected` (a protected path anywhere denies the whole patch);
  unit tests pin simulation semantics and both rollback outcomes;
  integration tests pin the one-approval journey, refusals, and the widened
  TOCTOU guard.

## Rollback

Remove the tool registration and the two eval cases; the workspace methods
and port types are additive and inert without the registration.
