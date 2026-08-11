# Haven — project card

A one-page summary for interviews and portfolio review. Every number below comes
from a command in this repository; nothing is estimated.

## Summary

| | |
|---|---|
| **What** | An evidence-driven, replayable TUI coding agent for a local Git repository |
| **Core problem** | A non-deterministic model proposes actions that become deterministic, security-sensitive filesystem and process side effects |
| **Thesis** | The model may only *propose*; deterministic code owns permission, execution, and the definition of success |
| **Role** | Sole author: product definition, async runtime, provider adapter, tool/permission layer, Textual TUI, durable recovery, eval suite |
| **Stack** | Python 3.12 · asyncio · Textual · Typer · Pydantic v2 · HTTPX · SQLite/aiosqlite · uv |

## The five mechanisms worth discussing

1. **Single execution channel.** Registry → strict schema → program-collected
   workspace facts → deterministic policy → exact approval → `ExecutionTicket` →
   executor. The executor accepts only a program-minted ticket, never model JSON.
2. **Digest-bound, single-use approval.** An approval pins workspace + tool +
   canonical args + preimage + preview. Any drift invalidates it; consumption is
   a conditional SQL `UPDATE`, so it can never be replayed. The preimage is
   re-verified after the human decides (TOCTOU guard).
3. **Evidence Gate.** A run that edited files cannot succeed on the model's word;
   it needs a diff *and* a passing check recorded after the last write, plus a
   deterministic review of the added lines (secrets, conflict markers, debugger
   statements, blanked files). Evidence is sequence-stamped so a stale pre-edit
   pass does not count.
4. **Durable execution with conservative recovery.** SQLite checkpoint +
   append-only event journal + execution journal. An interrupted effect is
   classified against preimage/postimage digests; anything unprovable is
   `EFFECT_UNKNOWN`, blocks resume, and is **never** auto-replayed.
5. **Reproducible offline eval as a security gate.** 27 scripted cases run the
   real stack with only the model faked; unauthorized file changes and transcript
   leaks fail the build.

## Measured results

Reproduce with `uv run pytest -q`, `uv run haven eval --offline`, and
`uv run coverage run -m pytest && uv run coverage report --include="src/*"`.

| Metric | Value |
|---|---|
| Automated tests | **335** passing |
| Line coverage (`src/`) | **88%** (domain + contracts ~100%) |
| Offline eval cases | **27/27 passed**, **0 security violations**, ~1 s wall clock |
| Live eval (DeepSeek `deepseek-v4-flash`) | **7/8 task cases**, **0 security violations**, **89% prompt-cache hit** (up from 71%) |
| Eval categories | task 8 · security 7 · robustness 5 · injection 3 · budget 2 · recovery 2 |
| Dedicated security tests | 13 path-escape / protected-path, plus 7 security and 3 injection eval cases |
| Recovery tests | 8, covering not-run / confirmed / ambiguous / abandoned / identity-mismatch |
| Static gates | `ruff`, `mypy --strict` (54 modules), `import-linter` (3 layering contracts) |
| Trace determinism | golden trace stable across runs; TUI and headless produce identical traces |
| Source / test size | ~7.9k / ~4.2k lines |

The live figures come from repeated runs of eight cases against one model and are
**not** a benchmark; they are an existence proof plus a cost figure. Task success
at scale is not measured and therefore not claimed. See `docs/EVAL_LIVE.md`,
which documents the six defects only a real model exposed — including a
cache-defeating context layout whose fix raised the prompt-cache hit rate from
71% to 89% on the same suite — and `eval_report/prompt-comparison.md` for the
measured cost of the last context change.

## Trade-offs I chose, and why

| Chose | Over | Why |
|---|---|---|
| Deterministic diff review | A model Reviewer subagent | Its §11 gate requires measuring defect detection and false positives; with a scripted model those numbers would be authored, not measured (ADR 0007) |
| No MCP client | Runtime-discovered tools | It breaks the invariant that every tool is compiled in and provably classified, and improves no metric Haven measures (ADR 0007) |
| Explicit `while` loop + typed state machine | LangGraph | The loop, budgets, and recovery semantics *are* the project; a framework would hide exactly what I wanted to demonstrate |
| Registered check recipes (id only) | Arbitrary shell | Unbounded risk and unreproducible eval; the model can never supply a command string |
| Digest-bound one-time approval | Persistent broad grants | Easy to over-approve; drift must invalidate authorization |
| Program Evidence Gate | Model self-report | "Done!" is unfalsifiable; success must be an artifact |
| Block on ambiguous effects | Auto-retry the last step | Re-running a possibly-completed write is worse than asking a human |
| ScriptedModel as the default test model | Live-model CI | Free, fast, deterministic; live eval is separate and explicitly paid |
| Textual | Hand-rolled Rich loop | Workers, modal screens, and Pilot make the TUI testable offline |

## Known limitations (stated up front)

- Argv allowlist + env scrubbing + timeouts are process controls, **not** an OS
  sandbox. Haven assumes a locally trusted repo and does not claim it is safe to
  run malicious repository code; container/Seatbelt isolation is future work.
- Single repository, single provider, fixed recipes, no automatic Git history
  changes, no multi-agent, no RAG, no MCP.
- Token/cost accounting is exact when the provider reports usage and explicitly
  flagged `estimated` otherwise.

## Résumé bullets

Every number is from the measured table above.

- Built **Haven**, a Python/asyncio + Textual TUI coding agent implementing a
  bounded agent loop, a provider-neutral streaming adapter, and a
  `search → read → edit → verify → diff` repository workflow with hard budgets
  (steps/tools/time/tokens/cost), cancellation, stuck-loop detection, and exactly
  one stop reason per run.
- Designed a single `Registry → Schema → Policy → Approval → Executor` execution
  channel in which approvals are bound to a workspace/args/preimage/diff digest
  and consumed once; **27 offline eval cases including 7 security and 3
  prompt-injection scenarios hold unauthorized writes, approval bypass, and
  secret leakage at 0**.
- Implemented SQLite versioned checkpoints plus an append-only event journal with
  offline replay, classifying interrupted side effects by preimage/postimage
  digest and failing closed on ambiguity instead of replaying — covered by **8
  recovery tests across 5 interruption outcomes**.
- Enforced an "evidence, not assertion" success rule: a run that edits files only
  succeeds with a diff and a passing verification recorded after the last write,
  validated by **335 tests at 88% line coverage** with `mypy --strict` and
  `import-linter` layering contracts in CI.

## Interview prep

Questions I should be able to answer from code, tests, or reports:
why this is an agent and not a workflow; why a `ToolCall` is not permission; what
exactly an approval binds and why drift must invalidate it; how pre-existing
uncommitted user changes are protected; why "the model said done" is not success;
why provider-stream interruption and side-effect interruption recover
differently; what checkpoint vs. journal each solve; how repository prompt
injection is contained; why safety metrics are never averaged with quality ones.

Full list in `Haven_TUI_Coding_Agent_项目计划.md` §12. Failure analysis in
`docs/POSTMORTEM.md`.
