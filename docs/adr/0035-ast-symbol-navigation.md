# ADR 0035: lightweight AST outline and symbol navigation without an LSP

Date: 2026-10-22
Status: Accepted

## Context

Code exploration in Haven was previously confined to three primitives: `repo.list` (directory hierarchy), `repo.search` (regex text search via ripgrep), and `repo.read` (line-bounded slice reads).

In medium-to-large repositories, locating class hierarchies, function signatures, or symbol usages required the model to issue chains of 5–10 grep calls, wading through matching comments, docstrings, and tests. This consumed significant step and token budgets on purely structural orientation.

ADR 0023 evaluated and declined a full Language Server Protocol (LSP) daemon. That verdict remains sound: running a language server process (like Pyright, Rust-Analyzer, or gopls) in the background introduces heavy memory overhead, cross-platform setup requirements, language-specific runtime dependencies, and process crash surfaces that conflict with Haven's local, hermetic design.

What was missing was the lightweight middle ground: **in-memory, single-process AST analysis using the standard library.**

## Decision

**Ship `repo.outline` and `repo.symbol` as first-class, read-only tools.**

1. **`repo.outline(path: str)`**:
   - Parses the target file in memory (using Python's standard `ast` library for `.py`/`.pyi` files, heading extraction for `.md`, and pattern extraction for other languages);
   - Extracts top-level imports, classes (with docstring summaries, base classes, fields, and methods with argument and return type annotations), functions, and constants;
   - Allows the model to understand a file's public surface and architecture without reading its entire body into the transcript.

2. **`repo.symbol(name: str, path: str | None = None)`**:
   - Searches workspace source files for word-boundary matches of a programming identifier;
   - Categorizes matches into **definitions** (class, function, method, or assignment declarations) and **references** (usages, calls, imports);
   - Bounded by result caps and preview line truncation to prevent context blowup;
   - Filters out non-source directories (`.git`, `.haven`, `.venv`, `node_modules`, cache directories).

3. **Safety and Policy Invariants**:
   - Both tools are members of `READ_ONLY_TOOLS` in `haven.domain.policy`, automatically allowed by `evaluate_policy` when within the workspace;
   - Paths escaping the workspace or touching protected paths (`.haven`, `.git`) are denied by construction before execution;
   - `TOOL_VERSION` moves from `"5"` to `"6"`, and the golden trace is regenerated deliberately.

## Consequences

- The model can resolve "where is this class defined and where is it called?" in a single turn instead of a multi-turn grep hunt.
- No external processes, daemon sockets, or language servers are introduced; execution remains hermetic and completes in milliseconds.
- Preserves the single execution channel, pure-function deterministic policy, and Evidence Gate invariants untouched.
