# Codebase audit companion

Seven one-purpose summaries of the same code, written from different angles. They
exist because a single architecture document answers "how does it work" but not
"what may I name this", "where do I add a test", or "what is already known to be
fragile". Volatile counts are deliberately **not** copied here — the generated
metrics table in [`docs/PROJECT_CARD.md`](../PROJECT_CARD.md) is the only place
those live, and `scripts/refresh_metrics.py --check` fails the build if it drifts.

## The seven

| Doc | Read it when you want to know |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | the architectural style, the layer boundaries, and what must never cross them |
| [`STRUCTURE.md`](STRUCTURE.md) | the top-level map, the entry points, and how files are named and organized |
| [`STACK.md`](STACK.md) | the runtime, the dependency set, and why each choice was made |
| [`CONVENTIONS.md`](CONVENTIONS.md) | naming, error handling, and the local idioms the code actually follows |
| [`TESTING.md`](TESTING.md) | the test stack, the commands, and the pattern each suite type follows |
| [`INTEGRATIONS.md`](INTEGRATIONS.md) | every external system Haven touches, and the contract for each |
| [`CONCERNS.md`](CONCERNS.md) | the prioritized risk register: top risks, technical debt, security concerns, and fragile areas — **start here before changing policy, boundary, gate, or provider code** |

## Provenance and staleness

`CONCERNS.md` records the review date in its header and carries dated amendment
notes; the other six were written against the same baseline. Treat them as a
map, not a contract — when a doc and the code disagree, the code wins and the doc
is a bug. [`docs/LEARNING.md`](../LEARNING.md) states the full truth hierarchy.

The raw machine scan these were derived from
(`docs/codebase/.codebase-scan.txt`) is gitignored and not distributed, so a
fresh clone will not have it; the seven docs above are the reviewed artifact.
