# Building a Production-Grade Coding Agent — a course built on Haven

This is a self-paced course that teaches how to build a real coding agent by
reading and extending a real one. The textbook is this repository: every lesson
points at actual source files, actual architecture decisions (ADRs), actual
tests, and — where it matters most — actual failures found by running against a
live model.

Most agent tutorials teach you to call an LLM in a `while` loop. This course is
about everything that turns that loop into something you would trust near your
filesystem: a single audited execution channel, deterministic permission,
precise approval, evidence-based success, durable recovery, and reproducible
evaluation. That machinery — not the loop — is the actual job.

## Who this is for

- You can read Python and have written async code before.
- You have called an LLM API at least once.
- You want to understand *agent engineering*, not prompt tricks: the boundaries,
  the failure modes, and the judgment calls.

You do **not** need an API key. The entire course runs offline against a scripted
model; a real provider is used only in the two optional "live" exercises.

## What you will be able to do afterwards

- Explain why a model's tool call is a *proposal*, not permission, and design a
  channel that enforces that.
- Separate State, Context, Trace, and ModelResult, and say what each is for.
- Build a `search → read → edit → verify → diff` loop with hard budgets, a single
  stop reason per run, and stuck-loop detection.
- Bind an approval to an exact action so it cannot be replayed or drifted.
- Decide run success from evidence (a diff + a passing check + a clean review),
  never from the model's word.
- Recover an interrupted run without ever double-applying a side effect.
- Write a reproducible offline eval that is also a security gate, and reason
  about what only a live run can tell you.
- Apply a benefit gate before adding a feature, and write down why you *didn't*
  build something.

## How to use this repository as a textbook

Four teaching aids, used throughout:

1. **The source.** Every module lists the files it covers. Read them; they are
   small and deliberately single-purpose.
2. **The git history.** The commits are vertical slices in build order —
   `git log --oneline` reads like a table of contents, from `domain` to
   `perf(context)`. Each module names its commit(s) so you can `git show` the
   slice in isolation.
3. **The ADRs (`docs/adr/`).** Each records a decision, the options considered,
   and what was given up. Modules link the ADR that governs them.
4. **The tests.** They are the executable spec. When a module says "prove it to
   yourself," it points at the test that already does.

Recommended setup:

```bash
uv sync --locked
uv run pytest -q                 # 342 tests, ~30s, fully offline
uv run haven eval --offline      # 27 cases, 0 security violations
```

Read a module, open the files it names in one pane and the module in another, run
its exercises, then check yourself against the linked test.

## Module map

| # | Module | Haven layer | Governing ADR |
|---|---|---|---|
| 01 | [Mental models: proposal vs. authority](01-mental-models.md) | whole system | 0002 |
| 02 | [The provider contract and streaming](02-provider-contract.md) | `contracts`, `ports`, `adapters/providers` | 0001 |
| 03 | [Tools and the single execution channel](03-tools-and-the-execution-channel.md) | `application/tool_pipeline`, `domain/ticket` | 0002 |
| 04 | [Policy, exact approval, workspace confinement](04-policy-approval-security.md) | `domain/policy`, `domain/approval`, `adapters/workspace_fs` | 0002 |
| 05 | [The bounded agent loop and context engineering](05-agent-loop-and-context.md) | `application/run_service`, `context_builder` | 0006 |
| 06 | [The Evidence Gate and deterministic review](06-evidence-gate.md) | `domain/evidence`, `domain/review` | 0003, 0007 |
| 07 | [Durable execution: checkpoint, journal, recovery, replay](07-durable-execution.md) | `adapters/sqlite_session`, `application/recovery_service` | 0004 |
| 08 | [The TUI as a pure interface](08-tui-pure-interface.md) | `interfaces/tui` | 0001 |
| 09 | [Evaluation and cost](09-evaluation-and-cost.md) | `evalkit`, `docs/EVAL*.md` | 0005, 0008 |
| 10 | [Engineering judgment](10-engineering-judgment.md) | `docs/adr`, `docs/POSTMORTEM.md` | all |
| — | [Capstone: extend Haven](capstone.md) | — | — |

The modules build on each other; do them in order the first time. Modules
09–10 are the ones interviewers care about most and are the hardest to fake, so
do not skip them.

## A note on honesty

This course inherits the project's rule: every number is one you can reproduce,
and every "we don't do X" comes with a reason. When a lesson cites a figure (for
example, the prompt-cache hit rate rising from 71% to 89%), it also tells you how
it was measured and what it does *not* prove. Learn that habit; it is worth more
than any single technique here.
