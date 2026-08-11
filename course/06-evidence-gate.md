# Module 06 — The Evidence Gate and deterministic review

> Files: `src/haven/domain/evidence.py`, `src/haven/domain/review.py`
> Tests: `tests/unit/test_evidence_gate.py`, `tests/unit/test_review.py`,
> `tests/integration/test_agent_journeys.py`
> ADR: [0003 — evidence gate](../docs/adr/0003-evidence-gate.md),
> [0007 — subagents, MCP, and deterministic review](../docs/adr/0007-subagents-mcp-and-deterministic-review.md)

## Learning objectives

- Decide run success from **artifacts**, not from the model's claim.
- Order evidence so a stale pre-edit pass cannot count.
- Recognize an *unwinnable* gate and stop instead of nudging forever.
- Add a deterministic content review that costs nothing and catches the obvious
  disasters.

## The gate

A model will happily say "Done! I fixed it." whether or not it did. If your
program accepts that, your success rate measures the model's confidence, not the
repository. `evaluate_evidence_gate` in `evidence.py` is the program deciding
instead:

- A run that made **no** edits may succeed on a final answer alone.
- A run that **did** edit must have, recorded *after the last write*, both a
  `repo.diff` and at least one passing `repo.check`. A failing check, a missing
  diff, or a missing check blocks success.

Evidence entries are sequence-stamped, so a check that ran *before* the last edit
does not count. This ordering detail is the whole ballgame: without it, an agent
could pass tests, then edit again, and still be called successful.

When the model claims completion without sufficient evidence, the loop nudges it
(bounded) with the specific gate failure, then stops with `evidence_missing`
rather than reporting a false success.

## Unwinnable gates: stop, don't nudge

A live run burned its entire tool budget on a task that could never satisfy the
gate: the workspace had **no check recipe registered**, so the instant the model
edited a file, the gate demanded a passing check that could not exist. The loop
kept nudging toward an unreachable outcome.

The fix (read the `verification_available` branch in `evidence.py` and the
`terminal` field on `GateResult`): distinguish "try again" from "this can never
pass." A run with writes and no verifier fails *terminally* with
`verification_unavailable`, and the loop halts there with an accurate stop reason
instead of spending its budget and blaming it. The system prompt was also fixed
to stop instructing the model to run a check when none is configured.

The general lesson is worth more than the specific fix: **a gate that cannot be
satisfied must be detectable as such.** Any retry/nudge loop needs an
"unwinnable" exit, or it becomes a budget incinerator.

## Deterministic review: cheaper than a reviewer agent

ADR 0007 asked whether to add a model-driven Reviewer subagent. It was rejected
for now (its benefit gate can't be passed with a scripted model — the reviewer
would only "find" the defects its script authored), and the plan's own fallback
was adopted instead: a **deterministic** review of the diff.

Read `review.py`. It inspects only the lines a run *added* and flags:

- committed secrets (private-key blocks, AWS keys, `sk-…` tokens, hardcoded
  passwords), with a placeholder allowlist to avoid flagging `changeme`;
- merge-conflict markers;
- debugger leftovers (`breakpoint()`, `pdb.set_trace()`, `debugger;`);
- a file that lost >80% and >50 of its lines (a blanked file).

A finding blocks success with `review_failed` and is fed back so the agent can
fix it — exactly like a failed check. Because only *added* lines are examined,
pre-existing repository content can never trigger it. Cost: zero tokens,
sub-millisecond, no second model, no false-negative risk from sampling.

This is a recurring theme you should steal: for a well-defined class of defects,
a deterministic check beats a probabilistic one on cost, latency, *and*
reliability. Save the model for the fuzzy problems.

## Exercises

1. **Order matters.** Using the helpers in `test_evidence_gate.py`, construct a
   ledger with a passing check *before* the last edit and assert the gate does
   **not** pass. Then move the check after and watch it pass.
2. **Unwinnable.** Write an integration run with no recipes where the model
   edits a file; assert it stops at `verification_unavailable` after ~3 steps,
   not at budget exhaustion. (See `TestUnwinnableEvidenceGate`.)
3. **Fool the reviewer (you can't, easily).** Add a diff that inserts an AWS key
   and assert `review_failed`; then add one that only *removes* a line containing
   a secret and assert it does **not** flag (only added lines count).
4. **Extend review.** Add a check for an added line longer than, say, 5000 chars
   (a likely accidental paste). Write its test first.

## Self-check

- Why is "the model said done" insufficient, and what is the minimal evidence
  for a run that edited files?
- Why must evidence be sequence-stamped?
- Give the general rule an "unwinnable gate" teaches about retry loops.
- When is a deterministic check the *right* choice over a model reviewer?

## Further reading

- ADR 0003 (gate) and ADR 0007 (why not a reviewer agent).
- `docs/EVAL_LIVE.md` for the unwinnable-gate discovery.
- Commit `31fde25` (`fix(evidence)`) is the unwinnable-gate fix in isolation.
