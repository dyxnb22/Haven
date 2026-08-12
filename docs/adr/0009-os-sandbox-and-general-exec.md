# ADR 0009: An OS sandbox, and general command execution behind it

## Status

Accepted — benefit gate passed. The capability gap was concrete, and the
mechanism that closes it is enforced by the operating system and asserted by
tests that run real commands on both supported platforms.

## Gate: problem

Haven could run exactly one kind of process: a `repo.check` recipe whose argv
was written by the user in advance. Anything else — reproducing a failure,
building, listing what a script actually does — was outside the agent's reach.
Mainstream coding agents (Codex CLI, OpenCode) derive most of their capability
from a general shell tool, and make it safe with an OS sandbox plus an approval
policy rather than by restricting the command set.

The second problem was a claim Haven was making loosely.
`docs/PROJECT_CARD.md` listed "argv allowlist + env scrubbing + timeouts are
process controls, **not** an OS sandbox" as the project's top known limitation.
That limitation is fine while the only process is a user-authored recipe. It
stops being fine the moment a model can propose the argv.

## Gate: current baseline (measured before this change)

- 8 compiled-in tools; one of them (`repo.check`) can start a process, and only
  with a pre-registered recipe id.
- 27 offline eval cases, 0 security violations. The security cases covered
  *paths the agent must not touch* and *content it must not write* — not *what a
  process it starts may do*.
- No OS-level confinement anywhere: a recipe ran with the user's full
  privileges, bounded only by argv, a scrubbed environment, and a timeout.

## Gate: options

- *Keep recipes only.* Rejected: it leaves the largest capability gap between
  Haven and comparable agents unaddressed, and it does not fix the confinement
  gap for recipes either.
- *A general exec tool with approval but no sandbox.* Rejected outright. An
  approved `bash -c` with the user's full privileges makes every other
  guarantee in this project decorative: the policy could deny `repo.edit` on
  `.git` while the shell rewrites it freely.
- *Ship a container/VM runtime.* Rejected for now: a large dependency and a
  large operational surface, for isolation strength beyond what a locally
  trusted repository needs.
- **A general `repo.exec` confined by the platform's native sandbox, denied
  entirely where no sandbox exists.** Accepted.

## Decision

Add `repo.exec`, and put every child process — including existing check
recipes — behind an OS sandbox.

**argv only, never a shell string.** `RepoExecArgs.argv` is an array; pipes,
globs, and redirection are not interpreted. A model that wants shell semantics
must name the interpreter (`["bash","-c",…]`), which is classified as shell
passthrough, always asks, and is labelled high risk in the approval card. The
point is not that an interpreter is forbidden — it is that the user is told
when the visible argv no longer describes what will happen.

**Classification decides friction; the sandbox decides capability.**
`domain/exec_policy.py` is a pure longest-prefix table that marks obviously
read-only commands (`ls`, `cat`, `git status`, `find` without `-delete`/`-exec`)
as `safe_read`, shells and inline-code interpreters as `shell_passthrough`, and
everything else as `other`. Only `safe_read` skips the approval prompt. A
misclassification therefore costs a skipped prompt, not an escape, because what
the process can actually do is bounded by the OS profile either way. This is
the one auto-allow exception in the whole policy, and a test pins it so it
cannot widen silently.

**Fail closed with no override.** With no sandbox backend the policy returns
`DENY sandbox_unavailable`, and it does so for an *absent* fact as well as a
false one — an un-collected fact must never read as permission. There is no
configuration key, CLI flag, or environment variable that disables the sandbox,
deliberately: the one knob a frustrated user would reach for is the one that
voids the guarantee. Where exec is unavailable the system prompt says so, so
the model does not loop against a tool that always denies — the same defect
`docs/EVAL_LIVE.md` records for the Evidence Gate.

**Exec output is never evidence.** `_execute_exec` records no `CheckEvidence`,
so a run that edits files still cannot succeed without `repo.diff` and a
passing registered recipe. A model can run `pytest` through exec and read the
output, and it will still be told its work is unverified. Without this, the
model could satisfy "success" with `echo ok`, and ADR 0003 would be decorative.
Promoting an approved command into a registered recipe would restore evidence
status legitimately (the user explicitly authorized that exact argv), and is
deliberately left for later: it needs its own approval UI, its own persistence,
and its own eval cases.

**Reads are confined, not just writes.** `repo.exec` validates `cwd`, not the
paths inside `argv`, so a permissive read profile would let
`["cat", "~/.ssh/id_rsa"]` succeed — quietly undoing the boundary `repo.read`
already enforces. Codex CLI's read-only mode does allow whole-filesystem reads;
Haven does not, because "the agent cannot reach outside the workspace" is a
claim this project already makes elsewhere. The sandbox keeps system paths
readable and makes the user's home unreadable, and denies the network
unconditionally, so a command cannot both obtain a credential and send it
anywhere.

**One wrapping site.** `ProcessExecutor` holds the launcher and wraps both
`run_exec` and `run_recipe`; callers pass unwrapped argv and a `SandboxSpec`.
Two wrapping sites would mean two places a future change could forget. Check
recipes are therefore confined too, with an `allow_network` opt-in that only
user-authored config can set.

**Backends.**

| | macOS | Linux |
|---|---|---|
| Mechanism | Seatbelt (`/usr/bin/sandbox-exec`, generated SBPL) | Landlock (ctypes, ABI ≥ 4, helper re-execs the target) |
| Writes | workspace + scratch only | workspace + scratch only |
| Reads | all but `private_roots` | enumerated system roots + workspace |
| Network | all denied | TCP bind/connect denied |
| `.git` writes | denied by the kernel | denied by Haven's tool layer only |

The two express the same intent with opposite mechanics: SBPL rules are ordered
and last-match-wins, so it allows broad reads then denies the home directory;
Landlock grants are additive, so it never grants the home directory and instead
enumerates what it does grant.

## What this does and does not change

- **Unchanged:** the execution channel. `repo.exec` goes through the same
  Registry → Schema → Facts → Policy → Approval → Ticket path as every other
  tool; the sandbox is one more stage before the executor, not a bypass.
- **Unchanged:** what counts as success. The Evidence Gate is untouched.
- **Unchanged:** recovery semantics. An interrupted exec has no
  preimage/postimage to prove anything with, so it classifies as `unknown` and
  blocks resume, exactly as an interrupted `repo.check` already did (ADR 0004).
- **Changed:** the agent can now run programs, and every process Haven starts —
  new or pre-existing — is confined by the OS.

## Gate: metrics

- **Enforcement, not inspection.** `tests/security/test_sandbox_enforcement.py`
  runs real commands and asserts real denials: write outside the workspace
  blocked, write inside allowed, a planted private key unreadable and absent
  from output, TCP connect to a live local listener blocked, `.git` write
  blocked (macOS), timeout kills the process, output bomb truncated. It also
  asserts an ordinary command still works, because an over-tight profile is as
  much a bug as a leaky one. 8 tests, 0 skips on macOS.
- **CI runs both platforms.** The workflow is now a `ubuntu-latest` /
  `macos-latest` matrix, and prints the detected backend, so a missing backend
  is visible rather than a silent skip.
- **Eval suite: 27 → 31 cases, 0 security violations.** Four new cases, each
  asserting a policy-level outcome so it is deterministic on any runner:
  `exec-escape` (cwd outside the workspace → `outside_workspace`),
  `exec-protected` (cwd in `.git` → `protected_path`),
  `exec-shell-passthrough` (`bash -c "curl … | sh"` rejected → no effect), and
  `exec-no-evidence` (edit + diff + exec, then a success claim →
  `stopped` / `evidence_missing` / `missing_check`). The last one is the case
  that pins the central claim: with the diff already recorded, the only thing
  missing is verification, and exec does not supply it.
- **434 tests, 87% line coverage, `mypy --strict` across 62 modules,
  import-linter contracts unchanged at 3 kept.**

Coverage moved 88% → 87%. The cause is `landlock_launcher.py` at 40%: its
syscall path cannot execute on macOS, where these numbers were measured. The
Linux CI job exercises it. Reporting the lower figure rather than excluding the
file keeps the number honest.

## Gate: risks

- **A sandbox that does not confine.** The failure mode of a profile is to look
  correct and stop nothing, so no test here asserts on profile text; they run
  commands and check that the OS refused.
- **Read confinement is coarse.** Denying `$HOME` while allowing `/etc` and
  `/usr` stops credential theft from the obvious places, not from an unusual one
  (a secret under `/opt`, say). Stated plainly in `docs/SECURITY.md` rather than
  implied to be complete.
- **Platform asymmetry.** `.git` write protection is kernel-enforced only on
  macOS; on Linux, Landlock's subtree grants cannot express "the workspace
  except `.git`", so that line is held by Haven's tool layer as before. Landlock
  ABI 4 network control covers TCP only, so UDP and DNS may escape on Linux.
  Both are documented rather than smoothed over.
- **Not an isolation boundary against malicious code.** The profile allows
  `mach-lookup` and POSIX shared memory on macOS so ordinary interpreters run;
  IPC isolation is not a goal. Haven still assumes a locally trusted repository.
- **`sandbox-exec` is deprecated by Apple.** It is what Codex CLI ships today.
  If it is removed, `available()` returns false and exec fails closed.
- **Scope creep into a shell agent.** Bounded by what was deliberately left out:
  no background processes, no network, no evidence status, argv only.

## Rollback

Remove `repo.exec` from `ARGS_MODELS` and `EXEC_TOOLS`; the policy completeness
test then fails loudly until `exec_class` and `sandbox_available` also leave
`ToolFacts`. Restore `TOOL_VERSION = "1"`. Confining check recipes is a
separate concern and reverts independently by constructing `ProcessExecutor`
without a launcher.
