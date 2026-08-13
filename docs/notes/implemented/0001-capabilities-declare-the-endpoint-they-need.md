# A provider capability declares the endpoint it needs, not just a boolean

Date: 2026-08-13

## Context

`ModelProfile.supports_assistant_prefix` was a bare capability flag: "this model
can continue a truncated assistant message from its own prefix". Live probing
(recorded in ADR 0022) showed the capability is not a property of the *model* on
its own — DeepSeek honours `prefix: true` on `https://api.deepseek.com/beta` and
rejects it on the stable endpoint with an explicit 400.

So a bare `True` would have been correct for one deployment and a guaranteed
failure for another: every truncated turn on the stable endpoint would 400,
turning a working fallback into a broken run.

## Decision

The profile declares both the capability and the endpoint that satisfies it
(`prefix_beta_base_url`), and `prefix_continuation_enabled(base_url)` is the
guard. The composition root — the one place that knows the configured endpoint —
resolves it and passes the verdict to `RunService`, which keeps the shim when
the answer is no.

## Alternatives considered

- **Leave the flag off entirely.** Safe, but discards a confirmed capability
  that removes a whole extra request per truncation.
- **Flip the flag and document "point at the beta endpoint".** Makes a silent
  footgun out of a config mistake; the failure lands on the user as a 400 mid-run.
- **Have the adapter rewrite the base URL to the beta host when prefix is
  needed.** Overrides explicit user configuration from inside an adapter, which
  is exactly the kind of hidden defaulting the composition root exists to avoid.
- **Detect support at runtime by trying `prefix: true` once and falling back.**
  Spends a real request to learn a static fact, and the probe's failure mode is
  a 400 in the middle of a run.

## Consequences

A capability that depends on deployment facts now has one honest shape to
follow. The cost is that `ModelProfile` carries an endpoint string, which is a
deployment detail sitting in a model table — acceptable while exactly one
capability needs it, and a sign to introduce a deployment-capability type if a
second one appears.
