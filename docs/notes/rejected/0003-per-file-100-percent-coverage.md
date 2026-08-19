# Per-file 100% coverage as the merge gate

Date: 2026-08-13

Superseded on 2026-08-19 by
[note 0007](../implemented/0007-risk-tiered-coverage-floors.md): the 85% floor
now applies only to high-risk files, while the remaining core layer keeps a
70% collapse floor. The original decision is preserved below.

## Context

DeepSeek Harness enforces per-file 100% line/branch/function coverage on every
source file, with a reviewed exemption list ("100% or it doesn't merge... per-file
so a well-covered big file can't subsidize a bare one"). Haven published a
single project-wide figure, which has the exact weakness that comment names: 89%
overall says nothing about whether `domain/policy.py` is at 99% or 20%.

## Decision

Not adopted. Haven gates a **floor of 85% per file on the decision-making layers
only** (`domain`, `application`, `contracts`, `ports` — 42 files), via
`scripts/check_coverage_floor.py` in CI.

## Alternatives considered

- **Per-file 100% everywhere, as dsh does.** `sandbox/landlock_launcher.py`
  measures 40% on macOS because it is Linux-only code; `interfaces/cli.py` is
  64%. A 100% rule would encode a number no single platform can satisfy, and the
  honest way to reach it — exempting both files — is where the rule starts
  eroding. dsh can afford it because CI runs a platform matrix per package with
  an exemption mechanism that has its own membership contract; that is a large
  standing cost for a one-person repository.
- **Per-file 100% on the core layers only.** Closer, but it freezes today's
  numbers: every refactor that moves an untested branch becomes a coverage
  negotiation rather than a design discussion.
- **Keep only the overall percentage.** The status quo, and the thing that
  actually fails to catch a new file landing at 20%.
- **Raise the floor to today's worst gated file (87.5%).** Zero headroom, so the
  first legitimate refactor turns the gate red for no defect.

## Consequences

The gate catches a collapse, not a slow drift; a gated file sliding from 99% to
86% passes. That is the deliberate trade for a floor nobody has to argue with.
If the core layers ever cluster near 85%, the floor has stopped discriminating
and should be raised — the number is a tripwire, not a target.
