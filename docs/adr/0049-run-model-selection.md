# ADR 0049: Model selection and usage belong to the run

Date: 2026-11-07
Status: Accepted

## Context

The router previously chose a model after the request was built. A composite
profile supplied the request limits and could also price calls made by either
route. A session-level preset needs to apply to new turns while paused runs
remain recoverable with their original selection.

## Decision

Resolve the user-level preset or default route before starting a run. Save the
resolved model IDs, route threshold, preset name and a digest of the provider
endpoint and configured rate override in the checkpoint. Restore the saved
route even after a preset is renamed or edited. Require a matching digest before
generating or saving an answer that will resume execution. Credentials stay in
the configured environment variable. The catalog owns the clients for its
provider and closes them with the service.

Choose the model before building each request and use that choice through
retries. Use its profile for request limits, reasoning and pricing. In a route
that can switch models, native prefix continuation requires support across
the configured candidates and endpoint. Clear historical prefix flags at the
wire boundary. Keep the per-model overflow cap in the actor that discovered it.

Attach actual selection, usage and rate provenance to `model.completed`. The
event and the budget use the same normalized token counts. Track whether all
charged calls have a rate through checkpoints and child runs. The scalar USD
value remains the priced subtotal and budgets compare against that subtotal.

## Consequences

Configured user rates apply uniformly to a provider. Built-in public rates
apply only to their documented endpoints. The report separates token estimates
from rate-card estimates. Legacy checkpoints with prior charges and no pricing
provenance resume with unknown aggregate cost. Checkpoints without a saved model
selection use the current default. Older strict readers may reject additive
event and checkpoint fields.

See [model presets](../MODEL_PRESETS.md) for the supported request shapes and
the [dual-track router](0042-dual-track-fast-explorer-and-deep-coder-routing.md)
for routing rules.
