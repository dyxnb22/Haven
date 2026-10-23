# ADR 0040: AST-Guided Syntax Pre-commit Gate & Structured Diagnostic Feeder

Date: 2026-10-23
Status: Accepted

## Context

In an autonomous agent coding workflow, two major friction points degrade task efficiency and waste model reasoning budgets:
1. **Broken Syntax Pollution**: When the model proposes file modifications (`repo.edit`, `repo.create`, `repo.apply_patch`), subtle syntax slips (unclosed parentheses, mismatched indentation, missing colons, invalid JSON commas) were previously written directly to disk. This dirty state contaminated the workspace, triggered expensive roundtrips through external test runners, and prompted human review cards for code that was fundamentally broken.
2. **Noisy Process Diagnostics**: When test suites or linters fail in `repo.check` (or `repo.exec`), the model was presented with up to 4,000 characters of raw stdout/stderr. Test framework banners, collection statistics, and verbose tracebacks obscured the exact failure location (file, line number, failing assertion), causing models to consume extra tokens searching for the failure or to miss the actual cause.

## Decision

**Introduce an in-memory AST Pre-commit Syntax Gate and a Structured Diagnostic Feeder into the core tool lifecycle:**

1. **AST-Guided Pre-commit Gate (`syntax_gate.py`)**:
   - Before mutating files on disk via `repo.edit`, `repo.create`, or `repo.apply_patch`, the simulated postimage is parsed in-memory.
   - For Python files (`.py`, `.pyi`), `ast.parse` validates valid AST syntax and catches `SyntaxError` and `IndentationError`.
   - For structured configuration files (`.json`, `.toml`), `json.loads` and `tomllib.loads` validate structural integrity.
   - If syntax parsing fails:
     - The tool proposal fails closed immediately with `WorkspaceError("syntax_error", ...)` mapped to `ToolErrorCode.SYNTAX_ERROR`.
     - Zero bytes are written to disk; workspace files remain clean and untainted.
     - Human review cards are never prompted for unparseable code.
     - The model receives exact syntax diagnostics (file, line number, column, error description, and offending line snippet with caret pointer), enabling one-step recovery.
   - An optional `skip_syntax_check: bool = False` argument is supported across mutating tool schemas to permit deliberate bypass when authoring raw test fixtures or syntax highlighter tests.

2. **Structured Diagnostic Feeder (`diagnostic_feeder.py`)**:
   - When `repo.check` or `repo.exec` exits with a non-zero exit code:
     - High-signal error lines are extracted from stdout and stderr.
     - **Pytest**: Extracts test failure names and assertions from short summary info (`FAILED ...::... - AssertionError: ...`) and enriches them with exact failure lines from tracebacks.
     - **Linters / Typecheckers**: Extracts file, line, column, severity, and rule code for Ruff, Flake8, and Mypy.
     - **Python Tracebacks**: Extracts uncaught exception types, locations, and messages.
     - **Compilers**: Extracts file, line, and code for Rust/C compiler diagnostics.
   - Extracted items are attached to `ToolResult.payload["diagnostics"]` and formatted into `ToolResult.payload["summary"]`.
   - When checks succeed (`exit_code == 0`), payload structure remains untouched, preserving full deterministic trace stability.

## Consequences

- Broken AST mutations are caught before touching disk, saving execution cycles and preventing dirty workspace states.
- Human review is never requested for unparseable syntax.
- The model receives immediate, structured localization of test failures and linter errors, shortening debugging time and improving first-attempt repair rates.
- All architectural invariants (single `ToolPipeline` execution channel, deterministic policy, and Evidence Gate verification) remain strictly preserved.
