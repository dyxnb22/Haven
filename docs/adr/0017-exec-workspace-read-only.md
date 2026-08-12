# ADR 0017: repo.exec is workspace-read-only

Date: 2026-08-12
Status: Accepted (amends ADR 0009, extends ADR 0012/0013)

## Context

ADR 0009 gave `repo.exec` a sandbox profile with the workspace writable, on the
theory that generated artifacts (build output, caches) are part of running real
commands. Two facts, taken together, turned that into a hole on Linux:

1. Landlock grants are additive per subtree. A writable workspace cannot
   exclude `.git`, `.haven`, or `.haven.toml` — the kernel allows an approved
   `python -c` with `cwd="."` to rewrite `.git/config`. Seatbelt on macOS can
   and does deny those subpaths.
2. The before/after snapshot that attributes process writes (ADR 0012)
   deliberately excludes protected paths from its file walk, so such a write
   was not merely permitted — it was **invisible**: no evidence entry, no diff,
   no event.

Policy only validates `cwd`, not the paths a program touches internally, so the
tool layer could not see it either. The combination contradicted two published
guarantees: "protected paths cannot be modified" and "every mutation is
attributable".

## Decision

**Model-proposed exec loses workspace write access entirely.** The sandbox
profile for `repo.exec` is workspace-read-only; only the scratch directory is
writable (scratch is now always writable in both launchers — it exists so a
confined process has somewhere to write; the `writable` flag governs the
workspace alone). Real source changes must go through `repo.edit` /
`repo.create` / `repo.delete` / `repo.move`, which carry previews, preimage
pins, and evidence. This is the position the rest of the design already took:
the model proposes, deterministic code owns writes.

`repo.check` keeps a writable workspace — recipes are user-authored and test
suites legitimately write caches and artifacts — with its changes attributed by
snapshot (ADR 0012).

Two detection layers back the prevention:

- The attribution snapshot now digests the protected paths separately
  (`WorkspaceSnapshot.protected_digests`). Any process — exec under a
  non-enforcing launcher, or a trusted check on Linux — that changes `.git`,
  `.haven`, or `.haven.toml` produces an **error notice** in the event stream.
  The invisibility half of the hole is closed even where the kernel cannot
  prevent the write.
- `.haven` was added to the sandbox's protected subpaths, aligning the
  Seatbelt deny list with the workspace's `PROTECTED_COMPONENTS`.

## Consequences

- A `repo.exec` command that writes the workspace now fails with a permission
  error on both platforms. The eval case that previously demonstrated
  "an exec write is caught by the gate" (`exec-write-needs-evidence`) became
  `exec-write-is-blocked`: the file provably does not change. The gate-level
  attribution is still pinned separately with a non-enforcing launcher
  (`tests/integration/test_exec_evidence_hole.py`), because it is the safety
  net if a sandbox ever fails to enforce.
- Commands that legitimately generate workspace files (builders, formatters)
  do not fit `repo.exec` anymore. That is intentional: run them as a
  registered `repo.check` recipe, or let the model produce the change through
  the audited write tools.
- The ADR 0009 classification (SAFE_READ / OTHER) still governs approval
  friction, but no longer selects a write profile — there is only the
  read-only one.
- A trusted check on Linux writing `.git` remains possible at the kernel layer
  (Landlock cannot express the exclusion) but is now detected and reported as
  an error rather than silent. Preventing it outright would need a mount
  namespace or an isolated worktree; deferred until evidence shows it matters
  in practice.
