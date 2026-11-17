# ADR 0001: Language and Scope

## Status

Accepted, and **superseded in part**. Two lines below were true of the MVP and
are not true of the shipped system; the original text stays as the record of
where the project started:

- "six repo tools" — the surface grew to twelve in v1, and now comprises **twenty-one** compiled-in tools (`TOOL_VERSION = "7"`) plus namespaced external MCP tools (ADR 0035, 0037, 0038, 0039; current list derived from `KNOWN_TOOLS` in `docs/ARCHITECTURE.md`).
- "OS-level sandboxing is out of scope" — **reversed by ADR 0009**, which put
  every child process behind Seatbelt/Landlock and made confinement one of the
  project's central claims. ADR 0013, 0017, and 0026 refine it further.

**Amended 2026-11-16: the count in the first bullet above has itself drifted.**
The surface is now **twenty-five** compiled-in tools with `TOOL_VERSION = "8"`.
That bullet was accurate when it was written and is kept as the record of the
earlier correction. The authoritative list is the generated table in
`docs/ARCHITECTURE.md`, derived from `KNOWN_TOOLS` and pinned by
`scripts/gen_tool_table.py`, so it cannot drift again.

The stack choices and the rejected alternatives below still hold.

## Context

Haven needs a stack that supports async streaming, a rich terminal UI, strict contracts, local persistence, and offline testing — while remaining finishable by one developer in about ten weeks.

## Decision

- Language: Python 3.12+
- Package manager: `uv` with `pyproject.toml` and committed `uv.lock`
- TUI: Textual
- CLI: Typer
- Async: `asyncio` only (no Trio/AnyIO abstraction layer)
- HTTP: HTTPX `AsyncClient` for provider adapters
- Persistence: SQLite via `aiosqlite`
- Types: dataclasses/Enum in domain; strict Pydantic v2 at boundaries
- MVP scope: single agent, six repo tools, one OpenAI-compatible provider plus Scripted/Fake provider

## Consequences

- Fast iteration and strong testability for a portfolio project
- No multi-provider routing, MCP, RAG, or arbitrary shell in v1
- OS-level sandboxing is out of scope; README must state local-trust assumptions

## Alternatives considered

- **LangGraph**: hides loop and recovery semantics; rejected for the main runtime
- **Rich-only TUI**: more manual terminal management; rejected in favor of Textual Workers and Pilot tests
- **Rust like Morrow**: stronger performance but duplicates an existing learning project without new independent judgment
