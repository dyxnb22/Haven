# Design: long-horizon mechanics (sub-project B)

Status: approved direction (user delegated detailed decisions on 2026-08-12).
Follows `docs/superpowers/specs/2026-08-12-repo-exec-sandbox-design.md`, which
committed this scope.

## Problem

Two limits become real now that `repo.exec` makes longer tasks possible.

**1. Truncation destroys facts instead of condensing them.**
`ContextBuilder._fit_to_budget` replaces each oversized tool output with
`[tool output dropped to fit the context budget]`. The agent is left with N
identical stubs: it no longer knows it read `src/calc.py`, that its edit landed,
or that `pytest` passed — only that *something* was there. On a long run this is
the moment the agent loses the thread, which is exactly when it can least afford
to.

**2. One budget for every task shape.** 24 steps / 48 tool calls is sized for a
single fix-verify cycle plus retries (ADR 0006). A three-line question wastes
the ceiling; a genuine multi-file refactor hits it and reports
`step_budget_exhausted` instead of a real outcome.

## Non-goals

- Model-written summaries. Rejected again, on the same grounds as ADR 0006: a
  summary the model authored can invent facts that later turns treat as
  established, including permission-shaped ones. This is the invariant the whole
  project rests on and compaction will not be the thing that breaks it.
- Adaptive budgets. An agent that can extend its own budget has no budget.
- Background or parallel work.

## Decision 1: deterministic compaction

When the transcript exceeds the character budget, drop the oldest tool outputs
**entirely** and insert one program-assembled `run_digest` message in the
position where the first dropped message sat.

```
[ system rules ]         stable
[ AGENTS.md guidance ]   stable
[ Task: {goal} ]         stable
[ run_digest ]           <- inserted at the first dropped position
[ surviving transcript ] append-only
------------------------ cacheable prefix ends here
[ task plan ]
[ run status ]
```

### The digest is derived from the dropped messages, not from live State

This is the load-bearing decision. Rendering the digest from `RunContext` each
turn would change its bytes every time a file was read, invalidating the
prefix cache on every single turn — the exact defect ADR 0008 was written to
fix. Deriving it from the dropped span instead makes it a pure function of
those messages, so it stays byte-identical until the next compaction event, and
`ContextBuilder` stays a pure function of its arguments.

Extraction is structural, not textual. A tool message has the shape

```
<tool_output tool="repo.read">
{"status": "ok", "result": {"path": "src/calc.py", ...}}
</tool_output>
```

so the tool name comes from the attribute and the facts from the JSON body.
A message that does not parse contributes a generic counted line rather than
raising: a malformed entry must not be able to abort a run.

### The digest is trusted, and that constrains what it may contain

`run_digest` is labelled `trusted`, because every field in it is
program-generated: paths normalized by `FsWorkspace`, digests computed by
Haven, exit codes from `ProcessExecutor`. To keep that label honest the digest
carries **only structured metadata** — never an excerpt of file content, never
a line of model prose. A test asserts that dropped file content cannot reappear
inside it, because the moment it could, the label would be a lie and repository
text would be laundered into trusted context.

Rendered form (bounded, one line per fact class):

```
Earlier steps, condensed by the program (originals dropped to fit the context
budget; these are recorded facts, not a summary):
- read: src/calc.py (a1b2c3d4), src/util.py (e5f6a7b8)
- edited: src/calc.py -> 9c8d7e6f
- checks: verify-calc exit 0
- other tool calls: 3 (repo.list, repo.search)
```

### What is dropped, and what is never dropped

Only `tool` messages are dropped, and the two most recent are always kept whole
— unchanged from today's policy and for the same reason (the model needs its
latest observations, and role-based protection survives the volatile tail).
Assistant turns are the model's own narrative of what it was doing and are
cheap; user messages carry gate feedback. Neither is ever dropped. The plan and
budget counters are re-rendered from State every turn and are structurally
un-droppable (ADR 0006).

### Cache consequence, stated plainly

A compaction event rewrites bytes at the digest's position, so the cache is
invalidated from there on that turn. This is unavoidable for any scheme that
reclaims context, and it is strictly better than the alternative: compaction is
deterministic, so subsequent turns re-derive the identical digest and the prefix
is stable again immediately. Model-driven compaction would rewrite the whole
transcript and could not promise that.

## Decision 2: budget tiers

Three named presets, selected with `haven run --tier`:

| Tier | steps | tool calls | wall | cost | For |
|---|---|---|---|---|---|
| `quick` | 8 | 16 | 180 s | $0.50 | a question, or a one-file fix |
| `standard` (default) | 24 | 48 | 600 s | $2.00 | today's behavior, unchanged |
| `deep` | 80 | 160 | 1800 s | $5.00 | multi-file work that compaction now makes survivable |

Tiers live in `domain/budget.py` as data, so the ceiling is still a constant in
the program rather than something a run can talk its way past. The merge order
is unchanged in spirit and made explicit: **built-in defaults → user config →
tier → project `.haven.toml` (tighten only)**. A tier is a user-level choice, so
it may raise; a repository still cannot raise anything.

## Testing

- Compaction unit tests: facts survive a drop; content does not; digest is
  byte-identical across two calls with the same transcript; a malformed tool
  message degrades to a counted line; the two newest tool outputs are kept.
- Prefix stability: two consecutive turns *between* compactions share a
  byte-identical prefix (extends `TestPrefixStability`).
- Tier tests: each tier's numbers; `--tier` reaches the run; project config can
  still only tighten a tier.
- Eval: one `long-horizon` case whose transcript is large enough to force
  compaction and which still reaches `evidence_satisfied`, proving the agent
  keeps the thread across a compaction.

## Rollback

Delete `application/compaction.py` and restore the stub branch in
`_fit_to_budget`; remove `BUDGET_TIERS` and the `--tier` option. No persisted
schema depends on either.
