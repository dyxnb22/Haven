# ADR 0011: Model profiles and cache-aware cost

## Status

Accepted. One part is a defect fix with a measured magnitude; one is a tuning
change whose stated justification the measurement corrected before it shipped.

## Gate: problem

**1. Reported cost was wrong, and ADR 0008 is what made it wrong.**
`Pricing.cost()` charged every input token at a single rate. DeepSeek v4 flash
bills a cache hit at $0.0028/1M and a miss at $0.14/1M — a factor of 50 — and
Haven's live suite already measures an 89% hit rate. Pricing all input at the
miss rate therefore overstated the input bill by about 7.8×:

```
true   = 0.89 × $0.0028 + 0.11 × $0.14 = $0.0179 / 1M
charged                                = $0.14   / 1M
```

`docs/PROJECT_CARD.md` presents cost as a measured figure. The error grew in
proportion to how well ADR 0008 worked, which is the worst shape for a metric
to have.

**2. The context budget was sized for a different model.** `MAX_CONTEXT_CHARS
= 96_000` is ~24K tokens against a 1M-token window: compaction fires far
earlier than this model requires.

**3. Two claims were untested.** Multiple tool calls per turn appeared to work
but nothing asserted it, and `reasoning_effort` — which the model defaults to
`high`, on the most expensive meter — was unmanaged.

## Gate: options

- *Keep one input rate.* Rejected: it is simply wrong, by a measurable factor.
- *Hard-code DeepSeek's numbers in the core.* Rejected: it would put a model
  name into the agent loop and silently misprice every other provider.
- **A `Pricing.cached_input_per_1m_usd` that defaults to unset, plus per-model
  defaults gathered as data in one module.** Accepted. With the cached rate
  unset the arithmetic is byte-identical to before, so no existing
  configuration changes meaning.

## Decision

`Pricing` moves to `domain/pricing.py` — it is pure arithmetic over token
counts and never belonged in `run_service` — and gains an optional cached rate.
`cost()` splits the input bill, clamping a cached count that exceeds the total
so a misreporting provider cannot produce a negative bill.

`application/profiles.py` holds `ModelProfile` values. `profile_for(name)`
returns `DEFAULT_PROFILE` for anything unrecognised, so an unfamiliar model
inherits Haven's historical behavior rather than numbers guessed from a
similar-sounding one. The flash profile carries the published rate card and a
480,000-character budget.

`ModelRequest` keeps no reasoning knob by default: no default is changed on a
guess, and the A/B harness exists to measure that question before anything is
claimed about it.

## The measurement that corrected this ADR before it shipped

The intended justification for the larger budget was: a hit costs 1/50 of a
miss, so retaining context is nearly free and compacting early wastes money.
`evals/ab.py` was written to demonstrate that, and **it showed the opposite**
on the per-turn number. Cached tokens are cheap, not free, so on the same
16-turn history the larger budget costs about **$0.000028 more** per
steady-state turn.

The defensible argument is narrower and quantified. A dropped file the agent
still needs must be read again: ~2,000 fresh tokens at the miss rate, about
**$0.000280**, plus a step and a tool call. So the larger budget pays for
itself at roughly **one avoided re-read every ten turns** — an easy bar on a
read-heavy task, but a bounded trade rather than a free win. The report says
that in those words, and the profile's comment does too.

This is recorded because the wrong version of the argument was persuasive, and
only building the measurement caught it.

## Gate: metrics

- Pricing: 7 unit tests, including one that pins the >7× overstatement the old
  arithmetic produced at the measured hit rate, and one asserting the unset
  default is unchanged.
- Profiles: 8 unit tests, including the fallback for unknown models and the
  invariant that the flash budget stays well inside the window.
- Parallel tool calls: an adapter contract test for interleaved delta assembly,
  plus 5 integration tests — order preserved, each call charged, each
  side-effecting call separately approved, a rejection not authorizing its
  sibling, and one failing call not aborting the others.
- Suite: **486 tests, 32 eval cases, 0 security violations.**

## What is deliberately not claimed

Every number above is offline and deterministic. Whether these changes improve
real task success, step count, or the live cache hit rate is unmeasured: it
needs paid runs against the real API. `docs/EVAL_LIVE.md` records the command
and marks those figures as not yet taken rather than carrying an estimate
dressed as a measurement.

The cost figures in `eval_report/ab-report.md` are computed from the published
rate card, not from an invoice. They are correct arithmetic on real prices,
which is not the same as an observed bill.

## Gate: risks

- **The rate card changes.** Prices are data in one module with a dated
  comment; a change is a one-line edit. Nothing else in the system knows them.
- **The larger budget increases first-turn latency**, because the first,
  uncached request is genuinely bigger. Bounded by staying at ~12% of the
  window, and the `quick` tier remains for short work.
- **Profiles could grow into per-model branching.** Contained by keeping them
  data in one module, consumed at exactly two points (context budget and
  pricing). A profile that needed a code path would be a signal to stop.

## Rollback

Remove `application/profiles.py` and the `max_context_chars` argument to
`ContextBuilder`; drop `cached_input_per_1m_usd` and the third parameter of
`cost()`. `cached_input_tokens` is already persisted and would simply stop
being priced.
