# ADR 0018: a process that tampers with a protected path fails the call

Date: 2026-08-12
Status: Accepted (extends ADR 0012/0017)

## Context

ADR 0017 made protected-path changes by any child process *detectable*: the
before/after snapshot digests `.git`, `.haven`, and `.haven.toml` separately,
and a change produces an **error notice** in the event stream. That closed the
invisibility half of the Landlock gap (a writable check workspace cannot
exclude `.git` at the kernel level on Linux), but left the outcome soft: the
tool call still *succeeded*, and a check recipe that rewrote the control plane
still recorded check evidence — meaning a green exit code from a tampering
check could satisfy the Evidence Gate. The second round of external audits
flagged the same asymmetry independently: everywhere else a boundary violation
is a hard failure; here it was an annotation.

## Decision

A process-caused change to a protected path fails the tool call with a new
error code, `protected_path_tampered`:

- **`repo.exec`**: the call returns the error instead of its output payload.
  (Defense in depth — the exec sandbox is workspace-read-only since ADR 0017,
  so this fires only where a launcher cannot enforce.)
- **`repo.check`**: the call returns the error and **no check evidence is
  recorded**. A recipe that rewrote the control plane is not a verification,
  so it can never satisfy the Evidence Gate, regardless of its exit code.

The error notice from ADR 0017 stays — the audit trail keeps the attribution —
and non-protected writes by a process are still attributed as edit evidence
exactly as before (ADR 0012).

## Consequences

- The locally-trusted-repo assumption for check recipes narrows in one
  specific way: a recipe may still write the workspace freely, but touching
  the control plane now costs it the run's trust rather than a log line.
- A legitimate recipe that (say) runs `git commit` as part of verification now
  fails. That is intentional: verification must observe the tree, not rewrite
  history; such workflows belong outside the agent's evidence channel.
- Regression coverage: `TestProtectedPathTamperIsDetected` in
  `tests/integration/test_exec_evidence_hole.py` pins both the exec and the
  check semantics, including the absence of check evidence.

## Rollback

Revert the tampered-return in `_record_process_writes`'s callers; the
detection and notices (ADR 0017) are independent and would remain.
