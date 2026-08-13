# ADR 0022: native prefix continuation and token-calibrated budgeting

Date: 2026-08-13
Status: Accepted. The pending live confirmation was carried out on 2026-08-13
and the capability is now **on for DeepSeek, bound to the endpoint that honours
it** — see "Live confirmation" below.

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

The flag stayed **off for DeepSeek until a live run confirmed** its beta
prefix-completion behaviour and endpoint. The adapter wire shape and the
run-loop branch are covered by offline tests (a contract test for the
`prefix: true` payload, an integration test for the prefix-vs-shim branch),
so the mechanism is real and tested — but a half-validated live path is not
shipped as the default, matching how ADR 0014 handled the reasoning 400.

## Live confirmation (2026-08-13)

Probed directly against the real API with `deepseek-chat`:

- `https://api.deepseek.com/beta` **extends the prefix in place**: a trailing
  assistant message `"1, 2, 3,"` with `prefix: true` came back continued as
  `" 4, 5, 6, ..."`, no seam and no reply turn.
- `https://api.deepseek.com` **rejects it**, with a 400 that names the
  requirement: `prefix is only available when using beta api (set
  base_url="https://api.deepseek.com/beta")`.
- The beta endpoint serves ordinary tool-calling unchanged (a `repo.read` call
  round-tripped through the wire-name mapping), so pointing a deployment there
  costs nothing else.

That 400 is the reason the capability is **endpoint-bound rather than a bare
flag**: enabling it globally would turn every truncated turn on the stable
endpoint into a guaranteed failure. `ModelProfile` therefore declares both
`supports_assistant_prefix` and the `prefix_beta_base_url` that satisfies it,
and `prefix_continuation_enabled(base_url)` is the guard. The composition root
resolves it against the configured endpoint and passes the verdict to
`RunService`; a deployment on the stable endpoint silently keeps the shim.

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

- Truncation recovery uses the native path on the DeepSeek beta endpoint and
  the shim everywhere else, decided by the endpoint guard rather than by hope.
- The char budget is provably inside the token window; a future budget change
  or model swap trips the guard test if it would not be.
- `evals/calibrate_context.py` is a reusable measurement, not a one-off.

## Rollback

Set `supports_assistant_prefix=False` on the profile (or clear
`prefix_beta_base_url` and point the deployment elsewhere) to use only the
shim; the `is_prefix` field and adapter branch are inert without it. The
token-window field and calibration are additive.
