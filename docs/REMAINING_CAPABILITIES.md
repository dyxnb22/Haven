# Session and execution capabilities

This work completes the five follow-ups to Plan mode and structured user input.

- [x] Real, bounded read-only scout/reviewer model loops with file scope and parent accounting.
- [x] Per-file and per-hunk review, with explicit approval before TUI worktree reconciliation.
- [x] New sessions, searchable selection, opening, continuation, and fork controls.
- [x] Stable plan step IDs, dependencies, blocked reasons, acceptance criteria, and evidence links.
- [x] Managed stdin and POSIX PTY support through the existing policy and approval pipeline.

## Child investigations

`agent.delegate` runs a separate model loop with its own transcript. Choose `scout`
for code investigation or `reviewer` for findings about changes. `file_scope`
optionally restricts every repository tool to listed paths; scoped calls must
explicitly name an allowed path. Children can list, search, read, outline, locate
symbols and inspect diffs. Nested delegation, user questions, external tools,
process execution and file mutations are denied independently by policy.

Child steps, tools, tokens and cost count against the parent. Requested child
budgets are bounded by the remaining parent budget, reserving a parent turn to
report results. Runs and lifecycle events are persisted; returned findings contain
actual model analysis and recorded read citations. Budget exhaustion returns an
incomplete/error result. Children share the configured provider and workspace,
not the parent's transcript. Read-only findings remain model judgments.

## Review and reconciliation

TUI worktree runs open a file/hunk browser after passing verification. Applying
confirms the complete snapshot; keeping or Escape retains the isolated worktree.
`/review` reopens it in that TUI session. `/diff [FILE]` browses recorded diffs.

For headless work, opt in with `--review`:

```bash
haven run "Fix parser" --workspace . --write --worktree --review --approval-policy all --json
haven review RUN_ID --workspace . --file src/parser.py --hunk 1
haven review RUN_ID --workspace . --apply --binding DIGEST
# Or remove the retained worktree explicitly:
haven review RUN_ID --workspace . --discard --binding DIGEST
```

Viewing prints a content binding; applying/discarding requires that exact value.
Views and resolution work after restarting and do not create a model client.
File/hunk filters only affect viewing. Applying reconciles all run-owned files,
checks both the verified worktree and main-workspace preimages again, and rolls
back earlier files if a later write fails. Existing destination file modes are
preserved; this is a file-content review. Unrelated main-workspace changes are
not included. Pending review state survives a user-question pause and is protected
from session GC. Content drift refuses reconciliation and retains the worktree.

Default headless runs retain automatic reconciliation after the same checks;
TUI confirmation and headless `--review` are explicit human review workflows.
Without worktree isolation, `/diff` still provides a recorded change browser.

## Sessions

`/new` clears conversation state and starts a fresh transcript and run diff.
Workspace, model, approval mode and selected skills remain selected.
`/sessions [QUERY]` searches IDs, goals and statuses across all stored user runs
in the current workspace, returning up to 100 matches. Open loads history without
replaying approvals or tools; Resume performs recovery checks for interrupted
runs; Fork uses the selected transcript for the next submitted message. Ordinary
messages after opening a finished session continue its conversation with a fresh
budget and evidence ledger. Delegated children are not selectable user sessions.

Active runs, unanswered questions and unresolved reviews block switching tasks.
Use `/answer` or `/review` to finish that interaction. CLI history is searchable
with `haven sessions list --search QUERY`; `show`, `continue`, and `resume` retain
their existing contracts.

## Plan acceptance

Steps support stable IDs, `depends_on`, `blocked_reason`, `acceptance` and
`evidence`. Validation is atomic and references only recorded reads, diffs and
fresh checks. See [Plan mode](PLAN_MODE.md#step-dependencies-and-acceptance).

## stdin and PTY

`task.start` uses stdin/stdout/stderr pipes by default. Set `terminal=true` for a
POSIX PTY; terminal stdout and stderr are merged. `task.logs` includes prompts
before a newline and bounds both the number and size of lines.

```json
{"task_id":"task_1","data":"yes\n","end_of_input":false}
```

Send this to `task.input`; include the newline when submitting a line. Input is
limited to 4096 characters per call and bound to its exact approval. `ask` and
`smart` ask for every input; `full` follows its usual automatic approval behavior.
Read-only, Plan mode and children cannot send process input. The original
workspace-read-only, no-network OS sandbox remains in force; environment variables
are allowlisted. `end_of_input=true` closes pipe stdin, or sends canonical-terminal
EOF markers for a PTY. Raw-terminal programs may interpret those markers themselves.
PTY support is a managed byte stream, not a full terminal emulator or job-control shell.

Processes belong to one run. End, pause, cancel and explicit stop clean up their
process groups and terminal descriptors. Resume does not resurrect processes;
start a new managed task when needed. PTY requires POSIX; pipe stdin is portable.

## Verification

Each part requires behavioral tests. Validation runs focused tests first, then
`uv run python scripts/gates.py --mode fast`, followed by `uv run pytest -q`.
Behavioral coverage lives in `test_real_subagents`, `test_diff_review`,
`test_session_picker`, `test_plan_acceptance` and `test_task_input`, including
approval, scope, budget, content drift, rollback, restart and real-process cases.
