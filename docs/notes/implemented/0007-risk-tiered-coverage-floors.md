# Use risk-tiered per-file coverage floors

Date: 2026-08-19

## Context

The original coverage gate applied an 85% per-file floor to every module in
`domain`, `application`, `contracts`, and `ports`. It protected policy code
well, but treated a protocol or data carrier as if a missed line weakened the
security and recovery guarantees. That encourages coverage-driven tests for
low-risk plumbing and makes harmless refactors negotiate the same threshold as
the execution boundary.

## Decision

Keep 85% for an explicit set of files that directly decide permission,
approval, execution routing, evidence, compaction, or recovery. Keep a 70%
per-file collapse floor for orchestration modules and the rest of the four core
layers; their mutation outcomes remain pinned by integration journeys.
Platform adapters and UI surfaces remain covered by their focused suites
rather than this portable per-file gate.

## Alternatives considered

- **Keep 85% across all four layers** — simple, but risk-blind and the source of
  the maintenance pressure this change addresses.
- **Gate only the high-risk list** — smaller, but a new core module could then
  land almost untested without affecting an already high project average.
- **Use only total coverage** — lets a large well-tested module subsidize an
  untested file, the original failure mode of the gate.

## Consequences

Security-sensitive files retain the existing tripwire. Ordinary core modules
gain refactoring headroom but cannot collapse below 70%. Adding or moving a
load-bearing guarantee now requires reviewing `HIGH_RISK_FILES`, making the
risk classification explicit instead of inheriting it from a directory name.
The full gate appends the separately executed offline eval to the same coverage
data, so removing its duplicate pytest wrapper does not discard those paths.
