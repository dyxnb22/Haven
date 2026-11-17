# Decision notes

The lightweight tier below [ADRs](../adr/). An ADR carries a load-bearing
architectural call and is worth its ceremony; most decisions are smaller than
that and, until now, had nowhere to live except a chat log no future reader can
consult. A note is one page, written in the same change as the code.

## When to write one

Write a note when someone could reasonably ask "why is it done this way?" and
the code alone does not answer:

- a non-obvious trade-off inside one module,
- an idea that was tried and abandoned,
- a convention adopted across a few files,
- a capability deliberately declared in an unusual shape.

Write an **ADR** only when the decision changes a load-bearing guarantee, layer
boundary, persisted-state contract, security boundary, tool surface, or the
definition of success, and would be costly to reverse. Deferrals, experiment
verdicts, local conventions, and future design sketches are notes. Write
**nothing** when the change is mechanical.

The active reading map and ADR admission rules live in
[`docs/ADR_INDEX.md`](../ADR_INDEX.md). An ADR should normally stay under 80
lines and link to measurements rather than copying an evaluation narrative.

## Lifecycle

A note lives in the directory matching its state and moves between them:

| Directory | Meaning |
|---|---|
| `implemented/` | in the code today |
| `rejected/` | considered and declined — **and also where a withdrawn verdict stays.** A verdict later voided by measurement is annotated at the top of the note, pointing at the report that voided it; it is neither deleted nor silently rewritten, because the attempt and the reason it failed are the part worth re-reading |

`proposed/` is a valid state in `scripts/check_notes.py` but is currently empty —
nothing is sitting in the argued-for-but-unbuilt state right now. `rejected/` is
the most valuable directory of the two that are populated, and the reason this
tier exists: it is what stops a rejected idea being re-proposed every few months
by someone who cannot see why it was dropped.

## The notes

**Implemented** — the reasoning behind choices that are in the code today:

- [0001](implemented/0001-capabilities-declare-the-endpoint-they-need.md) — a provider capability declares the endpoint it needs, not just a boolean
- [0004](implemented/0004-corrections-annotate-adrs-they-never-rewrite-them.md) — corrections annotate an ADR; they never rewrite it
- [0005](implemented/0005-a-recipe-supplies-its-own-toolchain-env.md) — a recipe supplies its own toolchain environment
- [0006](implemented/0006-a-pre-registered-gate-needs-a-robustness-clause.md) — a pre-registered gate needs a robustness clause
- [0007](implemented/0007-risk-tiered-coverage-floors.md) — use risk-tiered per-file coverage floors

**Rejected** — kept with the reasoning intact:

- [0002](rejected/0002-repetition-is-nudged-before-it-is-fatal.md) — repetition is nudged before it is fatal *(verdict withdrawn: the A/B never triggered — see the annotation on the note and `docs/EVAL_LIVE.md`)*
- [0003](rejected/0003-per-file-100-percent-coverage.md) — per-file 100% coverage as the merge gate

## Format

Start from [`TEMPLATE.md`](TEMPLATE.md). Four sections are required and
`scripts/check_notes.py` enforces them (it runs in the `notes` gate):

`## Context` · `## Decision` · `## Alternatives considered` · `## Consequences`

*Alternatives considered* is the one that earns the note its keep. A decision
with no record of what it defeated invites re-litigation.
