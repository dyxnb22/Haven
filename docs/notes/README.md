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
| `proposed/` | argued for, not built |
| `implemented/` | in the code today |
| `rejected/` | considered and declined, with the reasoning kept |

`rejected/` is the most valuable of the three and the reason this tier exists:
it is what stops a rejected idea being re-proposed every few months by someone
who cannot see why it was dropped.

## Format

Start from [`TEMPLATE.md`](TEMPLATE.md). Four sections are required and
`scripts/check_notes.py` enforces them (it runs in the `notes` gate):

`## Context` · `## Decision` · `## Alternatives considered` · `## Consequences`

*Alternatives considered* is the one that earns the note its keep. A decision
with no record of what it defeated invites re-litigation.
