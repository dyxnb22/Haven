# Technology Stack

## Core Sections (Required)

### 1) Runtime Summary

| Area | Value | Evidence |
|------|-------|----------|
| Primary language | Python (>=3.12, ruff/mypy target py312) | `pyproject.toml` (`requires-python`, `[tool.ruff] target-version`) |
| Runtime + version | CPython 3.12 (CI installs 3.12 via uv) | `.github/workflows/ci.yml` ("uv python install 3.12") |
| Package manager | uv (locked via `uv.lock`, build backend `uv_build`) | `pyproject.toml` `[build-system]`, `uv.lock` |
| Module/build system | src-layout package `haven`, console script `haven` | `pyproject.toml` `[project.scripts]`, `src/haven/` |

### 2) Production Frameworks and Dependencies

| Dependency | Version | Role in system | Evidence |
|------------|---------|----------------|----------|
| pydantic >=2 | lock: v2 line | Strict DTOs at every boundary (`contracts/`), tool arg validation | `pyproject.toml`, `src/haven/contracts/base.py` |
| textual | locked | TUI framework (`interfaces/tui/`) | `src/haven/interfaces/tui/app.py` |
| typer | locked | CLI framework (`interfaces/cli.py`) | `src/haven/interfaces/cli.py` |
| httpx | locked | Async HTTP client for the OpenAI-compatible provider adapter | `src/haven/adapters/providers/openai_compatible.py` |
| aiosqlite | locked | Async SQLite session store (journal/checkpoints/approvals) | `src/haven/adapters/sqlite_session.py` |
| platformdirs | locked | User config/data dir resolution | `src/haven/config.py` |
| pydantic-settings | locked | Declared in `[project.dependencies]` | `pyproject.toml` |

### 3) Development Toolchain

| Tool | Purpose | Evidence |
|------|---------|----------|
| ruff | FORMAT + LINT (line 100; rules E,F,I,UP,B,SIM) | `pyproject.toml` `[tool.ruff]` |
| mypy --strict | TYPE CHECK (packages=haven) | `pyproject.toml` `[tool.mypy]` |
| pytest (+pytest-asyncio auto, pytest-timeout) | TEST | `pyproject.toml` `[tool.pytest.ini_options]` |
| coverage | COVERAGE (no fail_under threshold configured) | `.github/workflows/ci.yml`, `pyproject.toml` |
| import-linter | ARCHITECTURE CONTRACTS (3 contracts) | `pyproject.toml` `[tool.importlinter]` |
| hypothesis | PROPERTY TESTS | `pyproject.toml` dev group; `.hypothesis/` cache (gitignored) |
| respx | HTTP mocking for provider contract tests | `tests/contract/test_openai_compatible.py` |

Dependency group `real-evals` (markupsafe, trio, wcag-contrast-ratio) exists only
so pinned third-party eval repos' test suites run green; Haven never imports them
(comment in `pyproject.toml`).

### 4) Key Commands

```bash
uv sync --locked
uv run ruff format --check . && uv run ruff check .
uv run mypy src
uv run lint-imports
uv run coverage run -m pytest
uv run haven eval --offline          # deterministic eval suite (security gate)
uv run python scripts/refresh_metrics.py --check   # generated-metrics drift gate
```

### 5) Environment and Config

- Config sources: built-in defaults -> user `config.toml` (platformdirs) ->
  project `.haven.toml` (tighten-only) -> CLI flags (`src/haven/config.py`).
- Required env vars: `HAVEN_API_KEY` (default) or the var named by
  `HAVEN_API_KEY_ENV`; optional `HAVEN_BASE_URL`, `HAVEN_MODEL`,
  `HAVEN_DATA_DIR`. No `.env.example` exists — vars are discovered from
  `src/haven/config.py`.
- Deployment/runtime constraints: local single-machine tool; `repo.exec`
  requires an OS sandbox backend (macOS Seatbelt / Linux Landlock ABI>=4),
  otherwise denied (`src/haven/bootstrap.py::select_launcher`).

### 6) Evidence

- `pyproject.toml`
- `uv.lock`
- `.github/workflows/ci.yml`
- `src/haven/config.py`
