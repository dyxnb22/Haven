# Haven

Haven is an evidence-driven, replayable, locally scoped TUI Coding Agent. The model proposes actions; the program enforces policy, approval, execution, and success criteria.

## What it does

In a local Git repository, you start Haven with a bounded coding task. The agent can search and read code, propose precise file changes, wait for your approval, apply changes, run controlled verification, and show streaming answers, tool traces, diffs, test evidence, budgets, and stop reasons in the terminal.

## Core journey

```text
Open a local Git repo
  → Enter a coding task
  → Agent searches, reads, and plans
  → Agent proposes precise file changes
  → TUI shows diff, risk, and approval scope
  → You approve or reject
  → Executor applies changes and runs fixed verification commands
  → Agent may fix failures within a bounded budget
  → TUI shows final diff, tests, cost, trace, and stop reason
```

## What this project proves

- A custom Provider adapter, tool calling layer, and bounded agent loop — not a prebuilt agent framework
- Clear separation of State, Context, ModelResult, and Trace
- A `search → read → edit → verify → diff` loop for code repositories
- Deterministic policy, exact approval, and workspace constraints over model-proposed side effects
- Streaming output, cancellation, recovery, checkpoints, and offline eval

## Non-goals (v1)

- Multi-agent orchestration
- RAG / GraphRAG
- Browser, computer use, voice, or image input
- Cloud accounts, multi-tenancy, or remote execution
- Arbitrary shell commands
- Auto commit, push, or PR creation
- Multi-provider routing
- MCP in the MVP

## Status

Early bootstrap. See `Haven_TUI_Coding_Agent_项目计划.md` for the full implementation plan and milestones.

## Design references

Haven is an independent Python implementation. [Morrow](https://github.com/dyxnb22/Morrow) is one design reference for principles such as a single tool execution channel, policy-bound approval, and replayable eval — Haven does not fork or copy Morrow source code.

## Quick start

```bash
# Install uv: https://docs.astral.sh/uv/
uv sync --locked
uv run haven --help
```

## Development

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
uv run lint-imports
```

## License

TBD
