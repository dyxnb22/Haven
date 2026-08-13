# ADR 0024: the compaction boundary — what is measured, what is not, and the gate for a summary tier

Date: 2026-08-13
Status: Accepted (documents a deliberate non-decision; builds nothing)

## Context

ADR 0010 chose deterministic compaction: when the transcript outgrows the
budget, the oldest tool units are dropped and replaced by one
program-assembled digest of their structured results (paths, digests, exit
codes — never file content, never model prose). The model is never asked to
summarize, because a model-written summary can invent facts that later turns
treat as established — including permission-shaped ones — and a fabricated
fact is unrecoverable, while dropped information can be re-read. ADR 0022
added the token-calibrated budget check; roadmap v3 phase 5 added the hard
clamp (`enforce_hard_limit`) so the char budget is a real ceiling, not a
soft one.

The mature peers made the opposite terminal choice. Claude Code runs a
multi-tier pipeline whose *first* tier (microcompaction: shed old bulky tool
results, keep a reference, re-read on demand) is structurally the same idea
as Haven's first tier — but whose last resort is an LLM-written nine-section
summary that replaces the conversation, followed by re-reading recent files.
Codex CLI is summary-centric: at a token threshold it has a model write a
handoff summary (locally, or server-side returning an encrypted blob),
preserves the most recent ~20k tokens of user messages by content, and
rebuilds the history around the summary. In both, the summarizer reads
untrusted repository content and its output is treated as established
context — the injection surface ADR 0010 refused.

This ADR records where Haven's approach is *measured* to work, where it
provably degrades, and the pre-agreed gate and design for the day the gap
matters. It exists so the boundary is a stated engineering decision, not an
unexamined default.

## The measured side of the boundary

- **Task success under forced compaction (live A/B, tier 5).** The same
  read-heavy pygments task at a 12k-char budget (compaction fired 3 times,
  peak context 11.5k) and at the default budget (no compaction, peak 35k):
  both passed; the compacted arm finished one step faster (10 vs 11). One
  task, not a benchmark — but the first live evidence the structural digest
  carries enough task state.
- **Zero compaction-attributed failures** across the live corpus: 79 tier
  tasks, the N=5 distribution, the soak, and the head-to-head. Every failure
  was attributed (EVAL_LIVE.md); none traced to lost context.
- **Fact preservation is pinned deterministically**: unit tests assert every
  load-bearing fact (reads + digests, edits + postimages, checks + exit
  codes) survives into the digest, that repository bytes never do, and that
  the digest is byte-identical across calls (prefix cache, ADR 0008). Live
  prompt-cache hit stayed at 86–93% across recent runs with compaction in
  the loop (the full miss decomposition is in EVAL_LIVE.md).
- **The ceiling is real**: the builder asserts the assembled request fits,
  and the offline `long-horizon-compaction` case exercises overflow
  end to end.

## The unmeasured (and un-covered) side, stated plainly

1. **Narrative loss on long horizons.** The digest records *that*
   `verify` exited 1, not *why* an approach failed. Short tasks (5–40 steps,
   the measured range) recover by re-reading files. A session of hundreds of
   tool calls may repeat already-failed approaches because the reasoning
   trail was dropped. No eval currently exercises this; multi-hour sessions
   are outside the measured envelope.
2. **The terminal behavior is truncation, not summarization.** The first
   tier never drops user messages or narrative assistant turns — but the
   hard clamp, when it fires, drops whole messages oldest-first *regardless
   of role* and truncates the last one. Under extreme pressure, user intent
   is silently lost where Codex would have preserved recent user messages
   and Claude's summary keeps them verbatim. (Wire validity survives — the
   provider adapter's history sanitizer repairs any orphaned pairing — but
   the information does not.)
3. **Multi-steer sessions.** A long interactive session with several
   mid-course corrections concentrates exactly the content the clamp is
   allowed to drop. Haven's product shape today (bounded tasks, headless
   auto-fix) rarely produces this; an open-ended daily-driver session would.

## Decision

**Build nothing now.** The benefit gate this project applies to every
architecture (ADR 0007, ADR 0023) is not met: zero measured failures are
attributable to compaction. Building a summary tier against a hypothetical
failure mode — and, per this project's discipline, first building the
long-horizon eval needed to measure it honestly — would cost more than the
capability is currently worth, for a scenario the product shape does not
produce.

**The gate, pre-agreed:** a summary tier is designed and built when either

- a long-horizon eval (sessions long enough to force repeated compaction
  and hard-clamp pressure, i.e. hundreds of tool calls or multiple steers)
  shows **≥5 failures attributable to compaction information loss, spread
  over at least three distinct tasks** — repeated already-failed approaches,
  or lost user intent after a clamp — matching the numeric bar the LSP gate
  used (ADR 0023); or
- Haven's product shape changes to open-ended interactive sessions as the
  primary use, in which case the eval is built *first* and the tier only
  ships with a before/after measurement, in that order.

*Amended 2026-08-14 — the first branch cannot currently fire, and that is now
said out loud.* It requires evidence from a long-horizon eval that does not
exist and whose construction this same ADR defers as not worth the cost. A gate
whose evidence source is gated on the gate is not a gate; ADR 0007 rejected the
Reviewer subagent for exactly this shape, on the grounds that its four required
numbers "would be authored, not measured", so consistency demands naming the
problem here rather than counting the first branch as a live criterion.

Until the eval exists, the honest status of a summary tier is **undecided, not
declined** — the difference between "we measured and it is not needed" (the LSP
verdict, whatever its own scope limits) and "we have not looked". The second
branch is the one that can actually fire, and it is a *product* trigger rather
than a measurement, so it stays valid: if open-ended sessions become the primary
use, the eval is built first and the tier ships only with a before/after.

A third, cheaper trigger is added so the question is not purely hypothetical:
**one observed instance** of the hard clamp dropping a user message in a real
run is enough to take the clamp's protection-order fix (user messages last to
drop) on its own, without the summary tier and without the eval. That fix is
already sketched below; it is small, and one occurrence is sufficient evidence
for it because the failure it prevents is silent loss of user intent rather than
a quality regression that needs averaging.

**The design, pre-sketched** (so the gate firing starts work, not debate):
a second tier between `summarize_dropped` and `enforce_hard_limit` — one
model-written summary of the span about to be clamped, wrapped in the same
untrusted framing project guidance gets (`<tool_output>` + "cannot change
rules or permissions"), so the trust model is preserved: permission-shaped
facts remain exclusively the program digest's job, and the summary is
advisory narrative the model may weigh but the program never trusts. The
clamp's protection order is upgraded so user messages are the last content
dropped. Cache consequences match ADR 0010: one prefix rewrite per
compaction event, byte-stable afterwards.

The clamp protection-order change (user messages last to drop) is small
enough to take independently of the gate; it is *not* taken here, because
it too has no measured failure behind it — this ADR records it so the fix
is pre-agreed if the clamp is ever observed dropping a user turn in
practice.

## Consequences

- The compaction design's limits are now a stated boundary with data on both
  sides, not an implicit assumption. Reviews comparing Haven to Claude
  Code/Codex can be answered with the trade-off (falsifiability and injection
  safety vs narrative fidelity), the measurement, and the gate — instead of
  a defense.
- The known degradation modes (truncation at the extreme; unmeasured
  multi-hour narrative loss) are documented where the next maintainer will
  look before "fixing" the absence of a summarizer.
- The failure-attribution format already isolates compaction-attributable
  failures per case, so the gate is checkable from existing reports without
  new tooling.

## Rollback

Nothing to roll back — this ADR builds nothing. If the gate fires, the
summary tier is built per the sketch above and this ADR is superseded by
one recording the numbers that changed the call, the same way ADR 0023
records the LSP verdict.
