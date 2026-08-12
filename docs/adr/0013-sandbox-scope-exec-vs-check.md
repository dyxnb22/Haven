# ADR 0013: The sandbox is mandatory for exec, defense-in-depth for check

## Status

Accepted — a clarifying decision. It records why two process-running tools are
sandboxed differently, so the asymmetry reads as a choice rather than an
oversight. No behavior changes; the code already did this.

## Gate: problem

A review flagged an inconsistency: `repo.exec` is denied when no OS sandbox
backend exists (ADR 0009), but `repo.check` runs a recipe whether or not a
backend is present. `ProcessExecutor` wraps both when a launcher is available
and runs both unwrapped when it is not. The question is whether check should be
made to fail closed like exec, or whether the difference is principled.

It is principled, and the reason is the difference in where the command comes
from.

## Decision

- **`repo.exec` — sandbox mandatory.** Its argv is proposed by the model. The
  sandbox is the only thing standing between a model-authored command and the
  machine, so running it unsandboxed would void the guarantee the tool exists
  to make. With no backend, exec is denied.
- **`repo.check` — sandbox defense-in-depth.** Its argv is a recipe id the user
  registered in trusted config, run against a repository the user pointed Haven
  at and therefore already trusts. Haven's whole model assumes a locally trusted
  repository. Sandboxing the recipe is valuable (it confines the repo's own test
  code, which is the one arguably-untrusted part), so it is applied whenever a
  backend exists — but requiring it would be stricter than the trust model, and
  would make Haven unable to verify anything on a platform without a backend,
  turning a safe tool into a dead one for no threat-model gain.

## Options considered

- *Make check fail closed like exec.* Rejected. It gains nothing against the
  stated threat model (the recipe and repo are trusted), it makes Haven
  read-only on Windows and older Linux kernels, and it creates an unwinnable
  loop: the Evidence Gate would demand a check that policy then denies. The
  earlier `verification_unavailable` work (ADR 0003/0006) exists precisely to
  avoid unsatisfiable gates.
- *Add a user opt-out to run exec unsandboxed.* Rejected outright. exec is
  model-proposed; there is no trusted-config story that makes an unsandboxed
  model command acceptable.
- **State the asymmetry precisely and pin it with tests.** Accepted.

## Gate: metrics

- `tests/integration/test_check_sandbox.py` pins both halves: a check goes
  through the launcher when one exists, exec is denied without a backend, and
  check's policy turns on recipe registration rather than backend availability.
- `SECURITY.md` §6a states the exec/check distinction and its consequence on
  unsupported platforms, replacing the earlier over-broad "every child process
  is wrapped".

## Gate: risks

- **A malicious repository's test code runs unsandboxed on a no-backend
  platform.** This is the pre-existing, documented assumption that Haven is not
  safe against untrusted repository code without a container/VM. It is not
  widened here; it is stated.

## Rollback

If check is ever made mandatory-sandbox, delete this ADR, add a
`sandbox_available` requirement to the check branch of `evaluate_policy`, and
extend the Evidence Gate's `verification_unavailable` terminal case to include
"no sandbox backend" so the loop stays winnable.
