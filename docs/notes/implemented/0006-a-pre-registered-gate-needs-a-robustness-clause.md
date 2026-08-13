# A pre-registered gate needs a robustness clause

Date: 2026-08-14

## Context

The repetition-nudge A/B (note 0002) was gated in advance on: *mean paired
delta ≤ −2 steps, **or** treatment pass count higher*. Both branches fired, so
the rule said keep, and keep is what happened.

Then the leave-one-out check showed the two branches were the same evidence.
Every control failure and the whole point estimate came from one case of seven;
without it, passes are 18/18 against 18/18 and the delta is −1.33. The `OR` had
looked like two chances to detect a real effect, and was actually one piece of
evidence able to trip two switches — because the quantities are correlated by
construction: a run that dies on `step_budget_exhausted` necessarily maxes its
step count *and* forfeits a pass.

The same shape is in two live gates. ADR 0023 deferred the LSP on "≥5 failures
attributable to semantic localization"; ADR 0024 gates a compaction summary
tier on "≥5 failures attributable to compaction information loss". Neither says
anything about how those five must be distributed. Five failures spread over
five tasks and five failures from one flaky task would both fire them.

## Decision

A pre-registered numeric gate must also state **how concentrated the evidence
is allowed to be.** Two clauses, both cheap to write in advance and cheap to
check afterwards:

1. **Leave-one-out.** The criterion must still hold with the single
   largest-contributing case removed. Where it does not, the outcome is
   *inconclusive* — not a pass, and not a failure either.
2. **Independence.** When a gate is an `OR` (or an `AND`) over several
   quantities, say whether those quantities can move independently. If a single
   run can trip more than one branch, the gate has one branch, and it should be
   written as one.

Applied retroactively as annotations, not rewrites: ADR 0023's and ADR 0024's
`≥5` bars now read as ≥5 **spread over at least three distinct tasks**, which
is the weakest concentration limit that rules out one flaky case carrying a
verdict.

## Alternatives considered

- **Require statistical significance instead.** Rejected as the primary clause:
  at seven cases the permutation test's floor makes almost any real effect
  non-significant (the nudge's p = 0.125 one-sided), so a significance gate
  would reject everything the corpus is large enough to detect. Leave-one-out
  asks the question that actually matters at this sample size — is this one
  case, or is it the mechanism?
- **Raise the sample size until significance is reachable.** Correct in
  principle, unaffordable per decision here: the A/B already cost 42 live runs
  for seven cases, and the corpus does not contain enough slow-converging tier-3
  tasks to grow it much. Better to state the imprecision than to price it out.
- **Re-decide the nudge under the new rule.** Rejected, and this is the one that
  matters: changing the criterion after seeing the data is exactly the failure
  pre-registration exists to prevent. The nudge stays, on the rule as written,
  with the weakness recorded. The new clause binds the *next* gate.
- **Leave it as a habit rather than a written rule.** The repository's own
  history says otherwise — ADR 0009's stale premise survived nine ADRs because
  a forward reference was a habit and not a gate (note 0004).

## Consequences

Gates get slightly harder to satisfy, which is the intent: the failure mode
being prevented is a verdict that reads as confirmed and is one unlucky case
wide. The cost is that a genuine effect concentrated in one task now reports
inconclusive and needs more data — an honest outcome that this project already
treats as a legitimate third result.

This does not reach backwards into decided verdicts. ADR 0023's LSP deferral
was a *failure to meet* a bar (≈1 against ≥5), and a concentration limit only
makes a bar harder to meet, so that verdict is unaffected in direction.
