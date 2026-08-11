# ADR 0001: Language and Scope

## Status

Accepted

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
