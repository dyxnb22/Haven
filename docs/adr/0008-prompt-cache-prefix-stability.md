# ADR 0008: Prompt-cache-friendly context ordering

## Status

Accepted — benefit gate passed. A cost optimization with a measured starting
point and a small, contained change.

## Gate: problem

The first live run (`docs/EVAL_LIVE.md`) spent **~26k input tokens per short
task** for runs of only 5–7 steps — the signature of re-sending most of the
context every turn.

Every OpenAI-compatible provider Haven targets (OpenAI, DeepSeek) caches on a
**stable prefix**: the longest run of leading tokens identical to a recent
request is billed at a discount and returned as `cached_tokens` /
`prompt_cache_hit_tokens`. The cache is automatic; it only helps if the prefix
actually stays stable turn to turn.

Haven's prefix did not. `ContextBuilder` put a live counter in the *second*
message:

```
Task: {goal}

(budget so far: step 3/24, tool calls 7/48)
```

`step 3/24` changes every turn. Prefix caching matches from the front, so the
first differing message invalidates the cache from there on — and that was
message two, sitting *before* the plan and the entire growing transcript.

## Gate: current baseline (measured)

- 71% cache hit on the 8-task live suite with the counter in message two.
- The system prompt and tool schemas (the largest fixed block) sit before the
  counter and cache regardless; what does *not* cache is everything after it,
  which is dominated by the transcript on later turns.

## Gate: options

- *Do nothing.* Rejected: a measured, avoidable cost with a one-line root cause.
- *Send provider-specific cache directives (Anthropic `cache_control`).*
  Rejected: these providers cache automatically; the problem is prefix
  instability, not a missing directive, and provider knobs would push wire
  concerns into the core.
- **Reorder context so everything volatile sits at the tail.** Accepted.

## Decision

Split the context into a stable head, the append-only transcript, and a volatile
tail:

```
[ system rules ]            stable for the whole run
[ AGENTS.md guidance ]      stable for the whole run
[ Task: {goal} ]            stable for the whole run
[ transcript... ]           append-only: old entries never change
--------------------------- everything above is a cacheable prefix
[ task plan ]               changes only when task.plan is called
[ run status: step/tools ]  changes every turn — must be last
```

- The goal message loses its counter and becomes just `Task: {goal}`.
- Budget counters move to a single trailing `run_status` message.
- The plan moves from between goal and transcript to *after* the transcript, so
  a plan update no longer invalidates the transcript cache.
- Truncation protects the two most recent **tool** outputs by role, since the
  newest output is no longer in the final slots.

The `run_status` message is `trusted` provenance (program-generated), unlike the
model-authored plan.

## What this does and does not change

- **Unchanged:** what the model sees. The same facts are present; only their
  order changes. The plan is still re-rendered from State every turn (ADR 0006),
  and budget is still visible.
- **Unchanged:** trust labelling and the State/Context/Trace separation.
- **Changed:** the leading bytes are now identical across turns until new
  transcript is appended — the shape prefix caching rewards.

## Gate: metrics

- Deterministic, asserted offline: two consecutive turns of the same run share a
  byte-identical prefix up to the end of the older transcript
  (`TestPrefixStability`).
- Cache accounting: cache hits are parsed into `Usage.cached_input_tokens`,
  recorded on `model.completed` and `run.finished`, surfaced in the CLI summary
  and the eval report's aggregate hit rate.
- Live before/after on the same suite and model:
  **71% → 89% cache hit, 127k → 114k input tokens** (`docs/EVAL_LIVE.md`).

## Gate: risks

- **A provider that does not cache.** Then this is a no-op: reordering is
  harmless and `cached_input_tokens` stays 0.
- **Moving the counter changes behavior.** The counter is advisory; the offline
  eval suite (stop reasons, file effects) and the golden trace guard against a
  regression. Live pass-count variance (5–8 across runs) is model
  non-determinism, not this change.

## Rollback

Revert the ordering in `ContextBuilder.build` and drop `cached_input_tokens`; no
persisted schema depends on the field (it defaults to 0).
