# ADR 0027: telling recoverable provider failures apart from terminal ones

Date: 2026-08-13
Status: Accepted

## Context

Every provider failure reaching `RunService` was, in practice, one of two
things: a transport blip the retry loop already handled, or a `protocol` error
that killed the run. That binary was too coarse for the model Haven actually
targets, and three cases were being misfiled.

**A context-window 400 was terminal.** `max_context_chars` is a *character*
budget checked against an *estimated* token window; ADR 0022 measured the ratio
from the live corpus and pinned the worst case at 1.0 chars/token. That check
proves the budget is safe for the transcripts we measured — it cannot prove it
for a transcript denser than any we have seen (CJK, base64, minified data all
bill worse per character). When the estimate is wrong the provider says so
precisely, with a 400 naming the context length. Haven's answer was to fail the
run, discarding a recoverable situation the provider had just diagnosed for it.

**A total-stream deadline punished healthy generations.** The adapter capped a
whole response at 120 seconds from the first event. DeepSeek in thinking mode
streams `reasoning_content` steadily for minutes on a hard task; that is a
*live* stream, not a stall, and the deadline killed it anyway. The failure then
looked like a timeout to retry, so the run paid for the same long think twice.

**`Retry-After` was ignored, and 402 was unreadable.** The retry loop used a
fixed exponential backoff even when the provider named a longer pause, and an
out-of-credit 402 surfaced as `unexpected provider status (402)` inside the
`protocol` bucket — indistinguishable, to a user reading the error, from a bug
in Haven's own request.

## Decision

**Give the recoverable cases their own codes, and recover from them.**

- **`context_overflow`.** The adapter recognises a context-length 400 at the
  wire boundary (the provider-specific phrasings stay in
  `openai_compatible.py`; the core only ever sees the code). `RunService`
  responds by shrinking the context budget — `ContextBuilder.reduce_budget`,
  forcing more history into the deterministic digest — and rebuilding the
  request, bounded by `MAX_CONTEXT_OVERFLOW_RETRIES`. The reduction sticks on
  the builder, so a run that overflowed once does not rediscover it every turn,
  and the builder floors it so the fixed head always fits. A transcript that
  genuinely cannot fit still stops the run, with the same stop reason as before.
- **Idle timeout instead of a total deadline.** The post-TTFT bound now applies
  to the *gap between* streamed events and resets on each one. A steady stream
  is never cut short; a genuine stall still times out and is still retryable.
  The bound is per-profile (`stream_idle_timeout_s`), generous for the
  reasoning model. The overall run stays bounded by the wall-clock budget,
  which is the ceiling that was always doing that job.
- **`Retry-After` is honoured** — `_retry_delay` takes the longer of the
  provider's request and the loop's backoff, capped by `MODEL_RETRY_MAX_DELAY`
  so one hostile header cannot park a run past a budget checked only between
  turns.
- **`quota`** names an out-of-credit 402, terminal and non-retryable, so the
  user reads "top up" rather than "Haven sent something malformed".

## What this does not change

Nothing here weakens a guarantee. Compaction stays deterministic and
program-assembled — overflow recovery *increases* how much the digest absorbs;
it never asks the model to summarize (ADR 0010). No new success path exists:
the Evidence Gate is untouched. Retries remain confined to the model call,
which has no side effects; a tool call is still never retried (ADR 0004).

## Evaluated against the corpus and not built

Three adjacent changes were considered while doing this and rejected on the
same benefit gate ADR 0007 and ADR 0023 apply, because the failure corpus does
not ask for them:

- **Spilling oversized tool output to the artifact store** instead of applying
  line/byte caps. Attractive in principle (caps lose information a re-read must
  recover), but no failure in the live corpus is attributed to a truncated tool
  result. Unlock condition: a run that fails with a root cause naming
  information lost to a cap.
- **Raising `max_context_chars` to use more of the 1M-token window.** The
  budget is a cost/latency choice, not a safety limit (ADR 0011), and ADR
  0024's A/B found forced compaction at a *12k* budget passed the same task one
  step faster — so compaction is not currently costing outcomes. Raising it
  would spend more per turn against no measured win. Unlock condition: failures
  attributable to compaction discarding something a re-read could not recover.
- **A dedicated `compaction.applied` event.** Compaction is already observable
  per step: `context.built` reports a `run_digest` segment with its size and
  reason, and the overflow path above adds an explicit notice naming the new
  budget. A second event would restate what the trace already carries.

## Gate: metrics

Offline and asserted by tests: a context-length 400 maps to `context_overflow`
while an unrelated 400 stays `protocol`; a single overflow is recovered and the
run succeeds; a persistent one stops after a bounded number of attempts; a
steady stream outlasts an idle bound that a total deadline would have breached;
a stalled one times out; `Retry-After` wins over backoff and is capped; 402 is
`quota` and non-retryable. Live (2026-08-13, `deepseek-chat`): connectivity,
tool calling, and a full read-only run all pass unchanged on this path.

## Rollback

Each piece is independent. Drop the `context_overflow` branch in `_drive` and
the run fails on overflow as before; restore a total deadline by passing the
idle bound as a shrinking budget; ignore `retry_after_s` in `_retry_delay`;
remove the 402 branch. The codes may stay in the taxonomy harmlessly.
