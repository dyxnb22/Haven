# ADR 0025: a run-scoped standing approval for byte-identical checks

Date: 2026-08-13
Status: Accepted

## Context

The fix/verify loop's normal shape re-runs the same registered check several
times in one run: red, edit, green — sometimes with more iterations. Under
ADR 0002/0003 every `repo.check` is `ask`, so an interactive session asks
the human to approve the *same* recipe invocation again and again. Approval
fatigue is a real security cost, not just a UX one: a human trained to press
`a` at identical cards stops reading cards, which is exactly the state an
attacker wants them in when a *different* card appears.

The general solution peers use — persistent per-tool "always allow" grants —
is the over-approval trap ADR 0002 rejected: a broad standing grant detaches
authorization from the specific action. This ADR takes the narrowest cut
that removes the fatigue.

## Decision

**Approving one `repo.check` covers byte-identical re-runs of that check for
the remainder of the same run.** Precisely:

- Eligibility is decided by the existing approval digest — workspace
  identity, tool name, tool version, canonical arguments (the recipe id),
  and the preview (the recipe argv). Anything that would change what runs
  changes the digest and re-asks. Recipes are loaded at bootstrap, so the
  argv behind a given digest cannot drift mid-run.
- Scope is one run, in memory (`RunContext.standing_check_grants`), and
  deliberately not checkpointed: a resumed or forked run asks once again
  before re-arming. Nothing persists across processes.
- Only `repo.check` is eligible. It runs a user-registered recipe against a
  locally trusted repository (ADR 0013's stance) and is the one tool whose
  legitimate repetition is structural. Writes, `repo.exec`, and patches
  always re-ask; nothing about their flow changes.
- Consent is informed at the moment it is given: the approval card says
  "approving also covers identical re-runs for the rest of this run".
- A rejection arms nothing. Only a consumed human approval creates the
  grant.
- The audit trail stays one-to-one: a standing-grant execution still mints,
  decides, and consumes a fresh single-use approval row (the store-level
  single-consumption invariant is untouched), emits `approval.decided`, and
  a `notice` names the grant so replay shows exactly which asks were
  skipped and why.

## Consequences

- A typical fix/verify session asks once per distinct check instead of once
  per invocation; every remaining card is news, which is what makes cards
  worth reading.
- Headless approval policies (`--approval-policy`) and the offline eval's
  auto-approver are unaffected in outcome (they never blocked on repeats)
  but gain the same journal shape.
- The golden trace changes once (the check card's summary text), regenerated
  deliberately with `HAVEN_UPDATE_GOLDEN=1`.
- Pinned by `tests/integration/test_standing_approval.py`: identical re-runs
  ask once and journal per-execution approvals; a different recipe re-asks;
  a rejection never arms the grant; write tools always re-ask.

## Rollback

Delete the standing-grant branch in `ToolPipeline._ask_approval`, the
registration line after consumption, and `RunContext.standing_check_grants`;
restore the check card's summary line. No persisted state depends on it.
