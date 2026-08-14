# Design: a DeepSeek v4 flash harness (sub-project C)

> **Historical record — implemented and later refined.** Current provider and
> context behavior lives in `application/profiles.py`, ADRs 0011/0014/0022/0027,
> and `docs/EVAL_LIVE.md`. Measurements below retain their point-in-time meaning.

Status: approved direction (user delegated detailed decisions on 2026-08-12).
Scope committed in `2026-08-12-repo-exec-sandbox-design.md`. Explicitly
single-model: multi-provider support is a permanent non-goal.

## What the model's published behavior actually says

| Fact | Value | Source |
|---|---|---|
| Context window | 1M tokens, shared by input and output | DeepSeek V4 docs |
| Max output | 384K tokens | DeepSeek V4 docs |
| Input, cache hit | $0.0028 / 1M | official pricing |
| Input, cache miss | $0.14 / 1M | official pricing |
| Output | $0.28 / 1M | official pricing |
| Cache | automatic, prefix-unit match from token 0; partial middle matches never hit | DeepSeek KV-cache guide |
| Default reasoning | thinking on, `reasoning_effort: high` | DeepSeek V4 docs |

Two of these change decisions Haven has already made.

## Problem 1: cost accounting ignores the cache, and is therefore wrong

`Pricing.cost()` charges **every** input token at one rate. Haven already
measures a 89% cache hit rate on the live suite, and on this model a hit costs
**one fiftieth** of a miss. Pricing all input at the miss rate overstates the
input bill by roughly 7.8× at that hit rate:

```
true  = 0.89 × $0.0028 + 0.11 × $0.14 = $0.0179 / 1M
billed as today                       = $0.14   / 1M
```

`docs/PROJECT_CARD.md` reports cost as a measured figure. A number that wrong
undermines the honesty the rest of the project is built on, and it became
wrong precisely because ADR 0008 succeeded at raising the hit rate.

**Decision.** `Pricing` gains `cached_input_per_1m_usd`, and `cost()` takes the
cached token count and splits the input bill. When the cached rate is left at
its default of `None`, behavior is unchanged, so no existing configuration
silently changes meaning.

## Problem 2: the context budget is sized for a model that no longer exists

`MAX_CONTEXT_CHARS = 96_000` is roughly 24K tokens against a **1M token**
window — about 2.4% of it. That was a reasonable default when it was written,
and against this model it compacts far earlier than it needs to.

The tempting argument is that a cache hit costs 1/50 of a miss, so retaining
context is free and compaction is pure loss. **The measurement does not support
that**, and `evals/ab.py` prints the correction: cached tokens are cheap, not
free, so the larger budget costs about **$0.000028 more per steady-state turn**
on the same history.

The real case is avoided re-reads, and it is quantifiable. When compaction
drops a file the agent still needs, re-reading it costs ~2,000 fresh tokens at
the miss rate — about **$0.000280** — plus a step and a tool call. The larger
budget therefore pays for itself if it avoids **one re-read every ten turns**,
which a read-heavy task clears easily.

So: raise the budget, but record it as a modest bounded trade rather than a
free win, and keep compaction as the thing that stops an unbounded transcript.

**Decision.** The character budget becomes a per-model value rather than a
module constant, defaulting to today's 96,000 so nothing changes for an
unconfigured provider, and set to 480,000 (~120K tokens, ~12% of the window)
for this model. Not larger: latency and the miss-rate cost of genuinely new
content still grow with size, and a 1M-token request would be slow and
expensive on its first, uncached turn.

This is the first time a Haven decision is deliberately model-specific, so it
is expressed as data on a profile rather than as a branch in the core.

## Problem 3: reasoning effort is unmanaged

The model defaults to `reasoning_effort: high`, and output tokens are the most
expensive meter at $0.28/1M. Haven's turns are short and heavily constrained by
deterministic gates, so maximum reasoning on every turn is not obviously worth
its price — but that is a claim to measure, not to assume.

**Decision.** `ModelRequest` gains an optional `reasoning_effort`, passed
through only when set, so the default remains whatever the provider chooses.
The A/B harness can then measure it. No default is changed on the basis of a
guess.

## Problem 4: multiple tool calls in one turn are unverified

The adapter's `_ToolCallCollector` accumulates a tool-call array and
`_handle_tool_calls` iterates over all of them, so this appears to work
already — each call passes through the full channel with its own policy
decision and its own approval. But nothing asserts it, so it could regress
silently, and "the agent handles parallel tool calls" is a claim the project
should not make untested.

**Decision.** No new code is expected. Add a contract test (the adapter
assembles two interleaved calls from one stream) and an integration test (both
execute in order, each with its own approval, and a rejection of the first does
not silently execute the second). If either fails, the defect is real and gets
fixed; if both pass, the claim is now pinned.

## The model profile

```python
@dataclass(frozen=True, slots=True)
class ModelProfile:
    name: str
    max_context_chars: int
    pricing: Pricing
    reasoning_effort: str | None = None
```

`DEEPSEEK_V4_FLASH` carries the numbers in the table above. A profile is looked
up by model name with a documented fallback to a conservative default, so an
unknown model gets today's behavior rather than a guess. Profiles are data in
one module, not branches spread through the core.

## Measurement, and what cannot be claimed here

The offline half is deterministic and lands with this change: `evals/ab.py`
compares named variants (context budget, reasoning effort, prompt wording) on
request size and estimated cost, writing a Markdown report.

The live half cannot be produced by this work. Real step counts, token splits,
and cache hit rates require paid runs against the real API with a key this
environment does not have. The command is documented and the report has a
place to receive the numbers, and until someone runs it, `docs/EVAL_LIVE.md`
will say so plainly rather than carrying an estimate dressed as a measurement.

## Testing

- Pricing: cached and uncached tokens are billed at their own rates; the
  default (no cached rate configured) is byte-identical to today's arithmetic.
- Profile: lookup by name, fallback for unknown models, and the invariant that
  the flash profile's budget stays well under its 1M window.
- Context budget: the builder honours a profile's budget; the default is
  unchanged at 96,000.
- Multi-call: adapter contract test plus loop integration test, as above.
- A/B harness: runs offline and produces a report with no network access.

## Rollback

Remove `domain/profiles.py` and the `max_context_chars` argument; restore the
module constant and the single-rate `Pricing.cost`. No persisted schema depends
on either — `cached_input_tokens` is already stored and would simply stop being
priced.
