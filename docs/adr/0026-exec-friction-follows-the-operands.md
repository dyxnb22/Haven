# ADR 0026: exec approval friction follows the operands, not just the program

Date: 2026-08-13
Status: Accepted (corrects ADR 0009)

## Context

ADR 0009 gave `repo.exec` an OS sandbox and a small table of obviously
read-only programs (`ls`, `cat`, `head`, `tail`, `wc`, `rg`, `grep`,
`git status|log|diff|show`, `find` without action flags) that are auto-allowed
without a prompt. The stated justification, written into
`domain/exec_policy.py`, was that classification "decides how much approval
friction a command gets, never what it is able to do: capability is bounded by
the OS sandbox, so a misclassification costs a skipped prompt, not an escape."

A security review of the whole repository (2026-08-13) found that premise is
true for **writes** and false for **reads**:

- the sandbox blocks writes outside the workspace and hides `$HOME`, but
  deliberately leaves the rest of the filesystem readable so ordinary
  interpreters start (ADR 0009, ADR 0013);
- `repo.exec` validates `cwd`, not the paths inside `argv` (stated in
  SECURITY.md §6a);
- exec `stdout` is returned to the model and appended to the transcript.

Composed, those three give a silent read of any non-`$HOME` file straight to
the model provider, with no human in the loop — `cat /etc/passwd`,
`grep -r secret /opt`. On Linux the sharpest instance is
`cat /proc/<parent-pid>/environ`: the child's environment is scrubbed to
`ENV_ALLOWLIST` (`adapters/process_executor.py`), but the **parent** `haven`
process still holds the user's full shell environment, and `/proc` is a
readable root in the Landlock profile. Any third-party secret the user
exported (`GITHUB_TOKEN`, `AWS_*`, another provider's key) could reach the LLM
provider and the local journal without an approval card ever appearing.

Severity was graded MEDIUM, not HIGH: `$HOME` — the main credential store — is
blocked, the headline `HAVEN_API_KEY` is already shared with that provider, and
the trust model is single-user local. It rises with whatever else the user's
shell exports.

## Decision

**Auto-allow a read-only command only while its operands stay inside the
workspace.** `classify_argv` now demotes a `SAFE_READ` match to `OTHER` — i.e.
ordinary exec, which requires approval — when any operand after the matched
prefix is absolute, starts with `~`, contains a `..` component, or hides such
a path behind `--flag=value`. The same demotion applies to `find`.

Three properties keep this cheap:

- **Syntactic and conservative.** An operand that is not a path at all (a grep
  pattern, a git ref, a `-n` value) never looks absolute, so testing every
  operand costs nothing. A pattern that happens to contain `..` costs one
  extra prompt — the failure direction is friction, never a silent read.
- **The program path is not an operand.** `/bin/ls` still classifies by
  basename; only `argv[1:]` past the matched prefix is inspected.
- **The common case is unchanged.** `cat README.md`, `grep -rn x src`,
  `find . -name '*.py'`, `git log --oneline` stay friction-free; a regression
  test pins that so the fix cannot quietly turn every read into a prompt.

Nothing about capability changes: the sandbox, `cwd` validation, and the
ticket path are untouched. This only moves where the human is asked.

## Consequences

- A prompt-injected model can no longer exfiltrate out-of-workspace file
  contents (including the parent process environment) without the user seeing
  and approving a card that names the exact command.
- Agents doing legitimate out-of-workspace reads now pay one approval each.
  Acceptable: reading outside the repository is not the tool's job, and the
  headless policies (`--approval-policy`) still decide unattended runs.
- The docstring premise in `exec_policy.py` is corrected in place, since it was
  the stale reasoning that permitted the gap.
- Coverage: `tests/unit/test_exec_policy.py::TestOperandsMustStayInTheWorkspace`
  (absolute, `..`, `~`, `--flag=path`, program-path-is-not-an-operand, and the
  in-workspace non-regression set) plus an end-to-end pipeline test that an
  escaping read reaches approval and starts no process when rejected.

## Rollback

Delete `_operand_escapes_workspace` / `_operands_escape_workspace` and the two
call sites in `classify_argv`; the table-based classification returns to
program-only. No persisted state depends on it.
