# Plan mode

Plan mode prepares a structured implementation plan while the workspace remains
read-only. The user reviews the plan before explicitly starting a new execution
run. Existing execution, approval, and Evidence Gate defaults stay the same.

## Usage

In the TUI:

```text
/plan Add validation to the parser
Please include the empty-input edge case
/implement
```

`/plan GOAL` starts a new planning run. Subsequent messages revise that plan while
remaining in plan mode. `/implement` confirms the current ready plan;
`/implement RUN_ID` confirms a specific saved planning run. `/mode ask|smart|full`
still controls tool approval and cannot grant write access during planning.

Headless:

```bash
haven run "Add validation to the parser" --plan --workspace . --json
haven continue RUN_ID "Include the empty-input edge case" --workspace .
haven implement RUN_ID --workspace . --approval-policy all
```

Use the latest planning run ID if you revise a plan. `implement` itself is the
explicit confirmation; its tool approval policy defaults to `reject`, matching
the existing headless write policy. Use `all` only when you want unattended tool
approval. Both stages use the original workspace directly so saved plans remain
available across processes; `--plan` cannot be combined with `--write` or
`--worktree`. TUI sessions retain their existing workspace configuration.

## State and boundaries

`AgentMode` (`plan` or `agent`) is independent of `PermissionMode` and
`ApprovalMode`. Planning selects read-only permissions, filters the tools exposed
to the model, and independently rejects side effects in the policy pipeline.
File changes, checks, recipe registration, command execution, and process
start/stop are unavailable. MCP tools are allowed only when the configured gateway
declares them read-only; other gateway restrictions still apply.

The existing `RunContext.plan` and `task.plan` tool remain the task list. A separate
`plan_status` tracks `drafting → ready → executing → completed`. The runtime marks
a planning run ready only after it finishes successfully with a structured plan.
Missing plans trigger bounded retries. Interrupted or failed drafts cannot be
implemented. Execution is marked completed only when the run succeeds and all
steps are done; checklist completion alone never satisfies the Evidence Gate.

Confirmation starts a new run with fresh budgets and evidence, retaining the
saved goal, transcript, and plan. It requires a ready planning checkpoint, the
same workspace identity, a compatible capability catalog, and a writable session.
Regular continuation and checkpoint recovery preserve plan mode. New checkpoint
and event fields have defaults so older records still load.

## Runtime API

```python
from haven.domain.enums import AgentMode

planned = await runtime.start_run("Add parser validation", agent_mode=AgentMode.PLAN)
checkpoint = await runtime.get_checkpoint(planned.run_id)
# Present checkpoint.plan for review. Call the next entry only on user confirmation.
executed = await runtime.implement_plan(planned.run_id)
```

The runtime API does not infer confirmation from model text or chat messages.
Calling `implement_plan` cannot elevate a runtime created with read-only
permissions. Plans are model-authored working memory and remain untrusted context.

## Step dependencies and acceptance

`task.plan` assigns `step-N` IDs when callers omit them, reusing IDs for unchanged
titles. Preserve returned IDs when renaming or reordering. `depends_on` references
IDs in the submitted plan; unknown dependencies and cycles are rejected atomically.
Dependencies must be done before a step becomes `in_progress` or `done`.
`blocked` steps require `blocked_reason`; a plan with blocked steps cannot be implemented.

`acceptance` lists the step's criteria. Marking such a step `done` requires
`evidence`: `read:path` refers to a recorded file read, `diff` to a diff after the
latest edit, and `check:recipe_id` to that recipe's latest passing result after the
latest edit. Invented, failed, and stale check references are refused. These are
provenance checks; the runtime does not infer that a criterion is semantically true
from an arbitrary passing command. Legacy steps without criteria still work.
All step details are included in subsequent model context and saved checkpoints.
