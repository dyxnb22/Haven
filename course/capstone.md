# Capstone — extend Haven, or build your own

You have read the machinery. Now prove you own it. Pick **one** track. Each is
sized for a focused weekend and each ends with the same standard the project
holds itself to: tests green, a named decision recorded, and every claim
reproducible.

Before you start:

```bash
uv sync --locked
uv run pytest -q && uv run haven eval --offline
git switch -c capstone/<your-track>
```

## Track A — Add a tool end to end

Add `repo.symbols` (list function/class definitions in a file) or `repo.stat`
(size, line count, digest without full contents). Touch every layer you learned:

1. **Contract** (`contracts/tools.py`): a strict args model; register it in
   `ARGS_MODELS` and add a description.
2. **Policy** (`domain/policy.py`): classify it. It's read-only, so it belongs in
   `READ_ONLY_TOOLS`. The completeness test will fail until you do — that's the
   point.
3. **Facts + execution** (`application/tool_pipeline.py`, `adapters/workspace_fs.py`):
   collect workspace facts and execute, returning a structured `ToolResult`.
4. **Tests**: a unit test for the workspace method, an integration journey via
   `ScriptedModel`, and — for `repo.symbols` — a security test that it refuses a
   path outside the workspace.
5. **Prove the invariant**: confirm a bad path yields `not_found`/`denied`, never
   an exception (Module 03).

Done when: `pytest`, `mypy`, `ruff`, `lint-imports`, and `haven eval --offline`
are all green, and `haven debug-context "..."` shows the new tool in the catalog.

## Track B — A new provider adapter

Implement `ModelPort` for a provider Haven has never talked to (Anthropic
Messages, Google, a local Ollama server — your choice).

1. Put it in `adapters/providers/`; the core must not change at all.
2. Map its wire format to the neutral `ModelEvent` union, including usage and any
   cache/reasoning fields it reports.
3. Handle its quirks *in the adapter* (Anthropic streams differently and uses
   `cache_control`; a local model may report no usage at all).
4. Contract-test it offline with `httpx.MockTransport` as in
   `tests/contract/test_openai_compatible.py`: text assembly, tool-call
   assembly, error mapping, and "the API key never appears in an error."

Done when: the contract tests pass offline, and (optionally) `haven
verify-provider --yes` succeeds against the real endpoint. Write down, in a short
note, which quirks you had to absorb — that note is the deliverable that proves
you understood the boundary.

## Track C — A durability drill

Make recovery earn your trust.

1. Reproduce all three interruption outcomes from `tests/recovery/`: edit
   provably-not-run, provably-confirmed, and ambiguous.
2. Add a *new* crash point of your own — e.g. a crash after `repo.check` started
   but before its result was journaled — and decide the correct classification.
   Is a process effect ever provably "not run"? Justify your answer in the test's
   docstring.
3. Confirm that no scenario you add can lead to an automatic replay of an
   ambiguous effect. If you can find one that does, you've found a real bug —
   write it up as a postmortem entry (Module 10).

Done when: your new recovery test passes and the "never auto-replay ambiguous"
invariant demonstrably holds for your crash point.

## Track D — A benefit-gated feature (the hard one)

Propose a capability the project deliberately deferred (context *summarization*,
a read-only sandboxed shell, LSP-based navigation) and take it through the full
discipline:

1. Write the one-page benefit gate (Module 10): problem, baseline **with a real
   measurement**, options, decision, metric, rollback.
2. If — and only if — the gate passes, implement the smallest version that moves
   the metric.
3. Measure before/after. Offline where deterministic; live and clearly labelled
   where not (Module 09).
4. Write the ADR, including what you gave up.

Done when: whether you shipped it or not, you can defend the decision with a
number. A well-argued "I measured it and it wasn't worth it" is a passing
capstone — that is the actual skill.

## The bar for all tracks

```bash
uv run ruff format --check . && uv run ruff check .
uv run mypy src
uv run lint-imports
uv run pytest -q
uv run haven eval --offline        # security violations MUST be 0
```

Then write a short `CAPSTONE.md`: what you built, the decision you recorded, the
number you can reproduce, and one thing you'd do differently. That document —
not the diff — is what you'd talk through in an interview.

## Where to go after

- Re-read `docs/PROJECT_CARD.md` and rewrite its résumé bullets in your own
  voice, backed by numbers *you* reproduced.
- Read the original plan (`Haven_TUI_Coding_Agent_项目计划.md`) end to end. You
  now have the context to see why each non-goal is a non-goal.
- Teach one module to someone else. It is the fastest way to find the parts you
  only think you understand.
