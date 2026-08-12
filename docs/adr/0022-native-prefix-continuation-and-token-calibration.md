# ADR 0022: native prefix continuation and token-calibrated budgeting

Date: 2026-08-13
Status: Accepted (native continuation behind a capability flag, off until confirmed)

## Context

Two Phase-5 (roadmap v3) tails.

**Output-truncation continuation.** A `finish_reason: length` answer is
continued by appending a user message ("continue from where you stopped").
This conversational shim works (proven across tiers 3–4) but can duplicate a
phrase at the seam and spends a full extra request. Providers that support
*prefix completion* (DeepSeek's beta mode) can instead extend the partial
assistant message in place — no seam, no reply turn.

**Context budget.** `max_context_chars` is a hand-set char count, but the
provider windows and bills in tokens. Nothing checked the char budget against
the real token window; it was a reasonable guess.

## Decision

**Native prefix continuation, behind a profile capability flag.**
`ModelProfile.supports_assistant_prefix` (default false) declares that a
provider can extend a trailing assistant message. When set, a truncated turn
re-sends the partial as an assistant message with `is_prefix=True`; the
adapter emits it on the wire with `prefix: true`, and the context builder
drops its volatile tail for that turn so the prefix is genuinely the last
message (nothing may follow a prefix the provider must extend). When unset —
including for `deepseek-v4-flash` today — the conversational shim is used
unchanged.

The flag stays **off for DeepSeek until a live run confirms** its beta
prefix-completion behaviour and endpoint. The adapter wire shape and the
run-loop branch are covered by offline tests (a contract test for the
`prefix: true` payload, an integration test for the prefix-vs-shim branch),
so the mechanism is real and tested — but a half-validated live path is not
shipped as the default, matching how ADR 0014 handled the reasoning 400.
Flipping the flag for DeepSeek also requires pointing the adapter at the beta
endpoint; that is deferred with the confirmation run.

**Token-calibrated budget check.** `ModelProfile.context_window_tokens`
records the provider's real input window. `evals/calibrate_context.py`
measures chars-per-token from the committed live event streams (pairing each
`context.built` size with the `model.completed` input-token count); a unit
test asserts that `max_context_chars`, at the densest observed ratio
(`MEASURED_MIN_CHARS_PER_TOKEN`), implies a token count under the window. The
measurement's verdict on the existing 480k budget: safe with wide margin
(≈270k tokens at the median ratio, ≤480k at the conservative floor, versus a
1M window). The constant did not change — but it is now measured and
CI-checked rather than asserted, which was the point.

## Consequences

- Truncation recovery is unchanged in production (shim), with a tested native
  path ready to enable per model.
- The char budget is provably inside the token window; a future budget change
  or model swap trips the guard test if it would not be.
- `evals/calibrate_context.py` is a reusable measurement, not a one-off.

## Rollback

Set `supports_assistant_prefix=False` everywhere (already the default) to use
only the shim; the `is_prefix` field and adapter branch are inert without it.
The token-window field and calibration are additive.
