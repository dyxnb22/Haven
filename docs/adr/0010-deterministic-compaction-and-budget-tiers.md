# ADR 0010: Deterministic compaction and budget tiers

## Status

Accepted — benefit gate passed for both parts. Compaction replaces a mechanism
that was actively destroying information; tiers are a small, contained change
whose default is byte-for-byte the previous behavior.

## Gate: problem

**1. Truncation destroyed facts instead of condensing them.**
`ContextBuilder._fit_to_budget` replaced each oversized tool output with
`[tool output dropped to fit the context budget]`. On a long run the agent was
left with a row of identical stubs: it no longer knew which files it had read,
whether its edit had landed, or whether the check had passed — only that
*something* had been there. That is the moment an agent loses the thread, and
it arrives exactly when the task is long enough to need it.

**2. One budget for every task shape.** 24 steps / 48 tool calls is sized for
one fix-verify cycle plus retries (ADR 0006). A three-line question never
approaches it; a genuine multi-file refactor hits it and reports
`step_budget_exhausted` instead of a real outcome.

`repo.exec` (ADR 0009) made both sharper by making longer tasks possible.

## Gate: current baseline

- `MAX_CONTEXT_CHARS = 96_000`. Every tool output beyond that became a stub;
  nothing recorded what the stub had replaced.
- 31 eval cases, none of which reached the context ceiling, so the truncation
  path had no end-to-end coverage at all — only unit tests of the stub itself.
- One budget, used by every run and every eval case.

## Gate: options

**Compaction**

- *Keep stubbing.* Rejected: it is the information-destroying behavior above.
- *Ask the model to summarize the dropped span.* Rejected for the third time
  (ADR 0001, ADR 0006, here). A summary the model wrote can invent facts that
  later turns treat as established, including permission-shaped ones
  ("the user approved this", "the check passed"). Deterministic dropping can
  only *lose* information, which the model can recover by reading again; a
  fabricated summary is unrecoverable and can drive a wrong approval.
- **Drop the oldest tool outputs and replace them with one program-assembled
  digest of their structured results.** Accepted.

**Budgets**

- *Keep one budget.* Rejected: it mismatches most tasks in one direction or the
  other.
- *Let the agent request more budget.* Rejected, restating ADR 0006: an agent
  that can extend its own budget has no budget.
- **Three named presets the user picks at the CLI.** Accepted.

## Decision 1: deterministic compaction

`application/compaction.py` provides `summarize_dropped(messages, limit)`,
which drops the oldest `tool` messages and returns the survivors plus one
digest, and `build_run_digest(dropped)`, which renders the digest. The digest
is inserted at the position of the first message it replaces.

**The digest is derived from the dropped messages, not from live run state.**
This is the load-bearing decision and it is not the obvious one. Rendering the
digest from `RunContext` each turn would have been simpler to write, but its
bytes would change every time a file was read, invalidating the prefix cache on
*every* turn — precisely the defect ADR 0008 exists to prevent. Deriving it
from the dropped span keeps `ContextBuilder` a pure function of its arguments
and keeps the digest byte-identical until the next compaction event.

Extraction is structural: a tool message carries
`<tool_output tool="repo.read">` around a JSON body, so the tool name comes
from the attribute and the facts from the parsed result. A message that does
not parse contributes a counted line rather than raising — one malformed entry
must never abort a run.

**The digest is `trusted`, and that constrains its contents.** Every field in
it is program-generated: paths normalized by `FsWorkspace`, digests computed by
Haven, exit codes from `ProcessExecutor`. To keep the label honest it carries
only structured metadata — never file content, never model prose. A test
asserts that dropped file bytes cannot reappear inside it, because the moment
they could, repository text would be laundered into trusted context.

**What is never dropped:** assistant turns (the model's own narrative of what
it was doing, and cheap), user messages (gate feedback), and the two most
recent tool outputs. The plan and budget counters are re-rendered from State
every turn and are structurally un-droppable (ADR 0006).

**Cache consequence, stated plainly.** A compaction event rewrites bytes at the
digest's position, so the cache is invalidated from there on that turn. That is
unavoidable for any scheme that reclaims context, and it is bounded: because
compaction is deterministic, later turns re-derive an identical digest and the
prefix is stable again immediately. A model-written summary would rewrite the
whole transcript and could promise none of that.

## Decision 2: budget tiers

| Tier | steps | tool calls | wall | cost |
|---|---|---|---|---|
| `quick` | 8 | 16 | 180 s | $0.50 |
| `standard` (default) | 24 | 48 | 600 s | $2.00 |
| `deep` | 80 | 160 | 1800 s | $5.00 |

`BUDGET_TIERS` lives in `domain/budget.py` as data, so a ceiling remains a
constant in the program: a run selects a tier, it cannot invent one. The merge
order is now explicit — **defaults → user config → tier → project
`.haven.toml` (tighten only)**. A tier is a user-level choice at the CLI, so it
may raise; a repository still cannot raise anything, and a test pins that.

`standard` is deliberately identical to the previous default, so adding tiers
moves no existing behavior. A test asserts that equality rather than trusting
it.

## What this does and does not change

- **Unchanged:** the model is still never asked to summarize anything.
- **Unchanged:** trust labelling, the State/Context/Trace separation, and the
  Evidence Gate.
- **Changed:** a compacted run retains its facts, and the default context
  ceiling can be raised deliberately for work that needs it.

## Gate: metrics

- 17 compaction unit tests: facts survive, content does not, output is
  byte-identical across calls, malformed input degrades, the two newest tool
  outputs are kept, assistant and user messages are never dropped.
- Prefix stability across a compaction is asserted directly
  (`test_the_prefix_survives_a_turn_after_compaction`).
- `long-horizon-compaction` eval case: a 39 KB module read three times really
  does overflow the 96 KB budget, and the run still ends `evidence_satisfied`.
  A companion integration test asserts a `run_digest` segment was actually
  emitted, so the case cannot silently stop exercising compaction if the
  fixture or the budget ever changes.
- Suite: **32 eval cases, 0 security violations; 465 tests.**

Not measured: whether compaction improves live task success. That needs paid
runs against a real model and is deliberately not claimed here — the same line
ADR 0006 drew for planning.

## Gate: risks

- **A digest that lies.** Bounded by construction: it can only contain fields
  the program produced. The "content never reaches the digest" test is the
  guard, and it is the test to keep if any others are dropped.
- **Compaction thrash.** If the surviving transcript still exceeds the budget,
  the next turn compacts again and the prefix moves again. Bounded in practice
  because the two protected outputs are the only large survivors; if this ever
  bites, the fix is a lower `MAX_CONTEXT_CHARS`, not a smarter summary.
- **`deep` makes a bad run expensive.** It raises the cost ceiling to $5.00.
  It is opt-in per invocation, the counters are still enforced, and stuck-loop
  detection still fires on repetition regardless of remaining budget.

## Rollback

Delete `application/compaction.py` and restore the stub branch in
`_fit_to_budget`; remove `BUDGET_TIERS`, `DEFAULT_TIER`, and the `--tier`
option, and drop the `tier` parameter from `load_config` and `build_services`.
No persisted schema depends on either.
