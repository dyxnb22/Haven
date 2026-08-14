# Codebase Structure

## Core Sections (Required)

### 1) Top-Level Map

| Path | Purpose | Evidence |
|------|---------|----------|
| `src/haven/` | The package: 7 layers (interfaces, bootstrap, application, domain, ports, adapters, contracts) + evalkit + sandbox helpers | `src/haven/__init__.py` package map |
| `tests/` | Unit, integration, contract, security, recovery, TUI (Pilot), golden-trace, and eval tests; current count is generated | `pyproject.toml` testpaths; README metrics |
| `evals/` | Offline eval cases (JSON, committed) + live real-repo suite builder + head-to-head harness | `evals/generate_cases.py`, `evals/real/build.py`, `evals/headtohead/harness.py` |
| `docs/` | Current architecture/security/eval/learning docs, immutable ADR history, executed roadmaps/specs/plans, and the `codebase/` audit | `docs/LEARNING.md`; `docs/` tree |
| `course/` | One derivation module plus 10 layer modules and a capstone | `course/README.md` |
| `scripts/` | Declarative gate graph, docs/ADR/notes checks, generated metrics/tool table, and offline demo | `scripts/gates.py`; `scripts/` |
| `.github/workflows/ci.yml` | Runs the single full gate graph on macOS and Linux after a locked dependency sync | `.github/workflows/ci.yml`; `scripts/gates.py` |
| `evals/real/repos|fixtures|cases|report*`, `evals/headtohead/runs/`, `.hypothesis/` | Gitignored work products: third-party clones, run artifacts, and property-test cache | `.gitignore` |

### 2) Entry Points

- Main runtime entry: `haven` console script -> `haven.interfaces.cli:app`
  (`pyproject.toml [project.scripts]`); bare `haven` opens the Textual TUI.
- Secondary entry points: CLI subcommands (`run`, `continue`, `init`, `gc`,
  `sessions`, `replay`, `resume`, `rewind`, `reconcile`, `export`, `discover`,
  `doctor`, `config`, `debug-context`, `verify-provider`, `eval`); eval
  scripts under `evals/` are run directly with `uv run python`.
- How entry is selected: Typer app with subcommands; no-subcommand callback
  launches the TUI (`src/haven/interfaces/cli.py::main`).

### 3) Module Boundaries

| Boundary | What belongs here | What must not be here |
|----------|-------------------|------------------------|
| `domain/` | Pure decision logic (policy, digests, budgets, evidence gate, transitions) | Any I/O, framework imports (import-linter contract "domain is isolated") |
| `ports/` | `typing.Protocol` interfaces the core depends on | Implementations |
| `contracts/` | Strict pydantic DTOs crossing boundaries | Behavior |
| `application/` | Use-case orchestration (run loop, tool pipeline, context, recovery, maintenance) | Direct adapter imports (contract: application never imports adapters) |
| `adapters/` | Port implementations (fs workspace, sqlite, provider, sandbox launchers, lease) | Cross-adapter coupling to interfaces |
| `interfaces/` | CLI + TUI presentation | Direct adapter imports (contract: interfaces reach adapters only through bootstrap) |
| `bootstrap.py` | The only composition root | Business logic |

### 4) Naming and Organization Rules

- File naming pattern: `snake_case.py` (e.g. `tool_pipeline.py`,
  `workspace_lease.py`).
- Directory organization: by architectural layer, not by feature.
- Import conventions: absolute imports rooted at `haven.`; no path aliases;
  layer `__init__.py` files re-export the public surface.

### 5) Evidence

- `src/haven/__init__.py` (package map + reading order)
- `pyproject.toml` (`[tool.importlinter]` contracts, scripts)
- `docs/ARCHITECTURE.md`
