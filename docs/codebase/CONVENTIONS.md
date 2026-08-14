# Coding Conventions

## Core Sections (Required)

### 1) Naming Rules

| Item | Rule | Example | Evidence |
|------|------|---------|----------|
| Files | snake_case modules | `tool_pipeline.py`, `workspace_lease.py` | `src/haven/` tree |
| Functions/methods | snake_case; private prefixed `_` | `_collect_facts`, `evaluate_policy` | `src/haven/application/tool_pipeline.py` |
| Types/interfaces | PascalCase; ports end in `Port`, protocols descriptive | `WorkspacePort`, `SessionServices` | `src/haven/ports/workspace.py`, `interfaces/tui/app.py` |
| Constants/env vars | UPPER_SNAKE; env vars prefixed `HAVEN_` | `MAX_CONTEXT_CHARS`, `HAVEN_DATA_DIR` | `application/context_builder.py`, `config.py` |

### 2) Formatting and Linting

- Formatter: ruff format (line-length 100) — `pyproject.toml [tool.ruff]`.
- Linter: ruff check, rules `E,F,I,UP,B,SIM`; bugbear immutable-call
  exceptions for Typer defaults.
- Types: mypy `strict = true` over every `src/haven` module; the current module
  count is generated in the summary metrics.
- Preferred command: `uv run python scripts/gates.py --mode fast`, which runs
  formatting, lint, strict types, architecture, and documentation contracts from
  the same graph CI uses.

### 3) Import and Module Conventions

- Absolute imports rooted at `haven.`; grouped stdlib/third-party/first-party
  (ruff `I`).
- Layer direction enforced by import-linter: domain isolated; application
  never imports adapters; interfaces reach adapters only via `bootstrap`.
- Layer `__init__.py` files document and re-export the public surface.

### 4) Error and Logging Conventions

- Errors: adapters raise typed errors with stable string codes
  (`WorkspaceError(code, ...)`); the pipeline maps them to `ToolErrorCode`
  enums and returns structured `ToolResult`s — raw exceptions never reach the
  model or the user (`application/tool_pipeline.py::_ERROR_CODES`).
- Logging: no logging library; the typed event journal is the log
  (`contracts/events.py`), rendered by sinks (TUI, ConsoleSink, JSONL).
- Redaction: exports mask values of env vars whose names end in
  `_API_KEY/_KEY/_TOKEN/_SECRET/_PASSWORD` (`interfaces/export.py`); config
  explain prints key presence, never values.

### 5) Testing Conventions

- Location: `tests/<category>/test_*.py`; categories: unit, integration,
  contract, security, recovery, tui, golden, eval.
- Mocking: no monkeypatch-heavy style — dependency injection via ports
  (ScriptedModel, MemorySessionStore, RecordingLauncher in
  `tests/integration/fakes.py`); provider HTTP mocked with respx.
- Coverage: the overall `src/` figure is generated; every file in the four core
  decision/boundary layers has an enforced 85% floor. UI/platform adapters are
  covered by their dedicated suites rather than that per-file floor.

### 6) Evidence

- `pyproject.toml` (ruff/mypy/pytest/import-linter config)
- `src/haven/application/tool_pipeline.py` (error mapping)
- `tests/integration/harness.py` (DI-based test style)
