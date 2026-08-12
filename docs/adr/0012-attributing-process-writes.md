# ADR 0012: Attributing workspace writes made by a process

## Status

Accepted — defect fix. The gap was proven by running a command, not by reading
the code, and the fix restores an invariant the project already claimed.

## Gate: problem

`repo.exec` (ADR 0009) runs a program inside a sandbox that permits writes to
the workspace. `repo.check` runs a recipe under the same sandbox. But only
`repo.edit` and `repo.create` write to the evidence ledger, and the Evidence
Gate decides "did this run change files?" from `ledger.has_edits`. So a file
changed by a process is invisible to the gate.

The consequence is not theoretical. A run that rewrote `src/calc.py` through
`repo.exec` was reported `succeeded / final_answer` with the gate returning
`no_writes` — a run that changed a file, accepted as a run that changed
nothing. That hollow-outs the project's central claim, and it is recorded as a
strict `xfail` in `tests/integration/test_exec_evidence_hole.py`.

## Gate: options

- *Trust the model to call `repo.edit` for real edits and only use `repo.exec`
  for read-only work.* Rejected: the gate must not depend on the model's
  cooperation. That is the whole premise.
- *Parse the command to decide whether it writes.* Rejected: undecidable, and
  the sandbox already declines to reason about argv for exactly this reason.
- *Use `git status` after each process.* Rejected as the primary mechanism: it
  couples the security path to git being present and the workspace being a
  clean repo, the same environmental coupling that Linux testing showed to be
  fragile. Not every workspace is a git repo.
- **Snapshot the workspace immediately before and after each process, and
  attribute any change to that process as edit evidence.** Accepted.

## Decision

`WorkspacePort.capture_snapshot()` returns a `WorkspaceSnapshot`:

- `digests: dict[str, str]` — every regular file under the root, by sha256.
  This is the gate-complete map: a change to any file, text or binary, moves a
  digest.
- `contents: dict[str, str]` — the diffable subset (valid UTF-8 under the edit
  size cap), so the run diff can render what changed.

Both exclude the protected components (`.git`, `.haven.toml`) and the ignored
directories already used by search (`__pycache__`, `.pytest_cache`,
`node_modules`, and the sandbox scratch dir among them). A check that only
writes bytecode caches therefore records no change, which is correct: it
changed nothing a human would call a change.

The pipeline wraps every process:

```
before = workspace.capture_snapshot()
run the process
after  = workspace.capture_snapshot()
for path, (preimage, postimage) in changed(before, after):
    workspace.register_run_original(path, before.contents.get(path, ""))
    ledger = ledger.with_edit(EditEvidence(seq, path, preimage, postimage))
```

`register_run_original` seeds the run diff's originals only if the path is not
already tracked, so a file a process touched now appears in `repo.diff`, and a
file previously edited through the tool channel keeps its true run-start
original rather than being reset to its pre-process content.

The change set is computed by a pure function over the two digest maps: a path
is changed if its digest differs, appeared, or disappeared. Deletion records an
empty postimage; creation records an empty preimage — the same convention the
existing edit/create paths use.

## What this does and does not change

- **Unchanged:** the model still cannot satisfy the gate by asserting success,
  and `repo.exec` output is still not evidence. This closes the reverse gap: a
  write *is* now evidence of a write, so the gate demands a passing check after
  it, exactly as it does for `repo.edit`.
- **Unchanged:** trust labelling. A process-caused change is program-detected,
  so it is as trustworthy as any other digest Haven computes.
- **Changed:** a run that mutates the workspace through any tool is now held to
  the same evidence standard.

## Gate: metrics

- The strict `xfail` in `test_exec_evidence_hole.py` flips to a pass: a run that
  rewrites a file through `repo.exec` can no longer end `succeeded /
  final_answer`; it must produce a diff and a passing check first.
- Unit tests for `capture_snapshot` (ignored dirs excluded, binary detected by
  digest, size cap) and for the pure change-detection function.
- An eval case: a `repo.exec` that edits a file, followed by a bare success
  claim, ends `evidence_missing`.

## Gate: risks

- **A check that mutates the tree** (a formatter invoked as a check) now records
  an edit whose seq is later than the check's own, so the gate would ask for a
  further check. This is a rare configuration and arguably correct — a
  verification step that rewrites source is not a clean verification — and it is
  documented rather than special-cased.
- **A very large repository** makes each snapshot hash every file. Processes are
  infrequent relative to reads, the ignored-directory exclusions remove the
  usual bulk (`node_modules`, `target`), and Haven targets one modest local
  repository. If this ever bites, the snapshot can move to a git-backed diff
  where a repo is available; the port boundary makes that a swap, not a rewrite.

## Rollback

Remove `capture_snapshot` / `register_run_original` from the port and the two
wrapping blocks in the pipeline. The ledger returns to counting only
`repo.edit` / `repo.create`, and the `xfail` marker goes back on the hole test.
