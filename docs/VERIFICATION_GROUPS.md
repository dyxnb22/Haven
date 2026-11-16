# User-selected verification groups

A selected group fixes which checks must pass in a run. Checks still use
`repo.check`, registered recipes, the existing execution path and approvals.
Declaring or selecting a group does not run commands automatically.

## Configuration and selection

Group existing recipe IDs in user configuration or the project's `.haven.toml`:

```toml
[verification_groups]
quality = ["test", "lint"]

[recipes.test]
argv = ["uv", "run", "pytest", "-q"]

[recipes.lint]
argv = ["uv", "run", "ruff", "check", "."]
```

Names have 1–100 non-space characters. A configuration can contain up to
32 groups; each group contains 1–32 unique, existing recipe IDs.
Project definitions replace the same-named user definition before selection.
`haven config explain` shows the resolved members. These are declared
requirements, not proof that the selected commands cover a task's business intent.

In the TUI, `/verify` lists groups and current results.
`/verify use quality` selects a group for the next run;
`/verify clear` explicitly removes the requirement for the next run.
Selection changes are blocked during an active task, pending question or review.
Existing task results remain visible until a new run starts.

For headless execution:

```bash
haven run "Fix the parser" --write --approval-policy all --check-group quality
haven continue RUN_ID "Verify the follow-up" --write --approval-policy trusted-recipe
haven continue RUN_ID "Only explain the result" --clear-check-group
```

`haven continue` remains read-only by default. An executing group needs
`--write` and appropriate approval; selecting a group cannot grant permission.
The same `--check-group NAME` and `--clear-check-group` options are available
on `haven implement`. They cannot be combined.

## Required evidence

Every required recipe must have its latest result:

- recorded after the last write in this run;
- run from the frozen recipe definition, without extra `repo.check` arguments;
- completed with exit code zero.

The TUI Evidence panel distinguishes `passed`, `failed`, `not_run`,
`stale` and `mismatch`. The last means the command definition or extra
arguments differ from the selected invocation. A filtered test run can help
diagnosis but cannot replace the configured full command. Failed retries do
not erase a failure; a later matching, passing retry can satisfy the check.

The selected scope is enforced even when the run makes no file changes.
Runs with edits also retain the existing fresh-diff, check and deterministic
review requirements. Ordinary exec output and model statements are not check
evidence. Temporary recipes cannot replace a required recipe during the run.

Plan mode saves the selection but defers checks until explicit implementation.
A ready plan does not certify its checks. Headless JSON for a selected group
includes `verification.group` and per-recipe statuses.

## Persistence and compatibility

The checkpoint stores the group, required IDs, definition digests and actual
check digests. Resume and answers preserve that selection. Missing or changed
recipe definitions refuse execution before an answer is consumed; restore
the original configuration to resume. Removing or narrowing the configured
group does not remove the saved task's requirements.
When opening a saved session, required-check statuses use the checkpoint's
evidence ledger, matching recovery. Later raw journal entries remain visible
in the timeline but do not certify the restored run.

New follow-up and implementation runs inherit the previous selection unless
the user explicitly chooses another group or clears it. Their evidence ledger
starts empty, so prior-turn checks cannot certify new work. SDK
`check_group=None` means inheritance on continuation, while `check_group=""`
explicitly clears a new turn. A fresh run with no selection has no group.

Old configurations and checkpoints default to no group and retain their
existing evidence rules. Old check evidence has no command digest and cannot
certify a newly selected requirement. Older Haven versions may reject the
additional checkpoint or event fields. See [ADR 0046](adr/0046-user-selected-verification.md).
