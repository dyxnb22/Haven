# ADR 0021: headless write mode and one-step discovery accept

Date: 2026-08-12
Status: Accepted (extends ADR 0013/0017)

## Context

Two productization gaps the audits named, both about closing a loop that
stopped one step short:

- `haven run` was hard read-only, so Haven could not enter CI, batch fixes, or
  any unattended workflow — the exact use `codex exec` serves.
- `haven discover` printed a `[recipes]` block for the user to copy by hand, so
  zero-config was "detect → quit → paste → restart" rather than "detect →
  accept → use".

Neither may weaken the invariant that every write goes through
Registry → Policy → Approval → Evidence, or that the model never supplies a
command string.

## Decision

**Headless write mode.** `haven run` gains `--write` (default off) and
`--approval-policy reject | trusted-recipe | all`, plus `--jsonl`. Read-only
stays the default and stays a *policy-layer* guarantee (mode `read_only`
denies every mutation regardless of the approver). With `--write` the run is
interactive-mode and an automated `HeadlessApprover` supplies the decision a
human would:

- `reject` — deny every approval (the run proposes but cannot mutate; useful
  to see what it *would* do);
- `trusted-recipe` — approve only registered `repo.check` recipes, so a
  pipeline verifies unattended but never mutates;
- `all` — approve everything (full unattended auto-fix).

`all` is reachable only with an explicit `--write`, so unattended mutation can
never be the accidental default. A headless `--write` run whose workspace
writer lease is held by another process (ADR 0020) exits with a policy error
rather than silently degrading — a CI caller must know it did not run writable.
The tool channel, approval binding, and Evidence Gate are untouched: this only
automates the yes/no.

**Discovery accept.** `haven discover --accept` writes the suggested recipes
into `.haven.toml`, appending new `[recipes.<id>]` tables and never
overwriting an existing one (the user's version wins). It is a minimal
appender, not a TOML round-trip, so it cannot mangle hand-authored config. The
model still supplies nothing; the human authorizes once and the result is an
ordinary registered recipe usable next run.

## Command-generated patches: covered, with a deliberate remainder

The audits also asked for a "command generates a patch → preview → audited
apply" channel for formatters and codegen. Most of that need is already met
and stays where it is:

- A **user-authored formatter/codegen runs as a `repo.check` recipe**, whose
  workspace writes are attributed to the evidence ledger (ADR 0012/0017) and
  shown by `repo.diff` — a reviewable, audited mutation today.
- **Model-authored multi-file edits go through `repo.apply_patch`** (ADR 0019):
  one reviewable diff, one approval, atomic apply.

The only uncovered sliver is a *model-proposed arbitrary command* whose
side effects become a patch. That directly contradicts ADR 0017 (model-
proposed exec is workspace-read-only, on purpose), and implementing it safely
needs a copy-on-write overlay of the workspace — heavy machinery. Consistent
with this project's rule of not building speculative architecture, it is
deferred until a measured task needs it, with the two mechanisms above as the
supported path in the meantime.

## Consequences

- Haven can now run in CI (`haven run --write --approval-policy all --jsonl`)
  with every mutation still evidence-gated and lease-guarded.
- Zero-config is a single accepted step.
- Regression coverage: `tests/unit/test_headless_approver.py` and the
  discover-accept CLI tests in `tests/test_cli.py`.

## Rollback

`--write`/`--approval-policy` and `--accept` are additive CLI surface; drop
them to restore read-only headless and print-only discover.
