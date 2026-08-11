# Module 05 — The bounded agent loop and context engineering

> Files: `src/haven/application/run_service.py`,
> `src/haven/application/context_builder.py`, `src/haven/domain/budget.py`,
> `src/haven/domain/stuck.py`, `src/haven/domain/transitions.py`
> Tests: `tests/integration/test_agent_journeys.py`,
> `tests/integration/test_provider_retry.py`, `tests/unit/test_context_builder.py`,
> `tests/unit/test_budget.py`
> ADR: [0006 — long-horizon planning and budgets](../docs/adr/0006-long-horizon-planning-and-budgets.md)

## Learning objectives

- Write a finite loop where **every run ends with exactly one stop reason**.
- Enforce hard budgets and detect stuck loops.
- Select context deterministically instead of accumulating it.
- Retry a model call safely — and know when you must not.

## The loop

`RunService._drive` is the loop: `Model → Tool(s) → Observation → Model …` until
the program decides to stop. Before each step it checks the budget; it charges a
step; it builds context; it streams the model; it either runs the proposed tools
or, on a final answer, consults the Evidence Gate (Module 06).

The discipline to copy: **there is one `_finish` and it always names a
`StopReason`.** Read the `StopReason` enum. `final_answer`,
`evidence_satisfied`, `evidence_missing`, `step_budget_exhausted`, `no_progress`,
`provider_error`, `cancelled`, `verification_unavailable`, … A run cannot dribble
to a halt in an unknown state; if you cannot name why it stopped, you have a bug.

The status transitions themselves go through `domain/transitions.py`, where an
illegal move *raises*. A state-machine bug fails loudly instead of silently
corrupting a run.

## Budgets are a ceiling the agent cannot raise

`domain/budget.py`: steps, tool calls, wall time, tokens, cost. A project
`.haven.toml` may only *lower* them (Module 07's config story), and nothing in
the loop extends them. ADR 0006 records the defaults (24 steps / 48 tool calls)
and, importantly, *how they were derived*: the minimum successful trajectory is
read/edit/create/diff/check/answer, plus ~3 fix-verify rounds. The first version
(12/24) cut runs off exactly when they would have recovered — and reported
`step_budget_exhausted`, hiding the real outcome. Lesson: size budgets to the
work, and treat a budget stop as a real, named result, not a shrug.

## Stuck-loop detection

`domain/stuck.py`: if the model proposes the same tool with the same arguments
and gets the same result three times, that is `no_progress` and the run stops.
Cheap, deterministic, and it fires regardless of remaining budget — so a model
spinning in place cannot burn your whole token budget before you notice.

## Context is selected, not accumulated

This is the heart of the module. Open `context_builder.py`. The context is laid
out as:

```
stable head:   system rules · AGENTS.md guidance · Task: {goal}
               transcript (append-only)
volatile tail: task plan · run status (step/tool counters)
```

Three ideas are doing work here:

1. **Trust labelling.** Every segment carries a source and a `trusted` /
   `untrusted` flag, emitted in the `context.built` trace event and visible via
   `haven debug-context`. Repository-authored `AGENTS.md` is a labelled
   *untrusted* user message — never part of the system prompt. (An earlier
   version put it in the system role and labelled it trusted; that was a real bug,
   see Module 10 and the postmortem.)
2. **Deterministic truncation.** When the transcript is too big, the oldest tool
   outputs are replaced with a stub — never a model-written summary, which could
   invent "facts" that later turns treat as established. The plan is safe because
   it lives in State and is re-rendered each turn, so truncation cannot lose it.
3. **Prompt-cache-friendly ordering.** Everything that changes turn to turn (the
   plan, the budget counters) is at the *tail*, so the leading bytes stay
   identical across turns and a provider's automatic prefix cache can reuse them.
   This is ADR 0008; Module 09 has the measured 71%→89% result and the honest
   caveats.

`tests/unit/test_context_builder.py::TestPrefixStability` asserts idea 3
directly: build turn N and N+1 and confirm the prefix is byte-identical up to the
new transcript.

## Retrying the model — carefully

Read the retry logic in `run_service.py`. A model call has no side effects, so a
*connection* failure is safe to retry — unlike a tool call, which is never
retried. The subtlety, learned from a live run: a mid-stream drop is *also* safe
to retry, because the assembled text and tool calls are local to the attempt and
never reach the transcript until the turn completes. The UI is told to discard
what it showed via a `stream.restarted` event. `tests/integration/test_provider_retry.py`
pins both the retry and its bounds.

## Exercises

1. **Name the stop.** Run three scripted journeys from `test_agent_journeys.py`
   and, for each, predict the `StopReason` before you look.
2. **Force a budget stop.** Write a scripted run with `Budget(max_steps=2)` and a
   model that never finishes; assert `step_budget_exhausted` and that it stopped
   at step 2, not later.
3. **Prove the prefix.** Extend `TestPrefixStability`: advance the budget counter
   between two turns and assert that only the trailing message differs.
4. **Reason about retry.** Explain why retrying a tool call would be unsafe even
   though retrying a model call is fine. Which invariant from Module 03 is at
   stake?

## Self-check

- Give three distinct `StopReason`s and the condition that produces each.
- Why is model-written summarization banned as a truncation strategy?
- Where does the plan live, and why does that placement matter twice (truncation
  *and* caching)?

## Further reading

- ADR 0006 (planning + budgets) and ADR 0008 (prefix ordering).
- Commit `958d98a` (loop), `9d8e684` (`perf(context)`), `a52b886` (retry).
