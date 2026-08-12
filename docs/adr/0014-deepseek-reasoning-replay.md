# ADR 0014: Replaying provider reasoning on tool-call turns

## Status

Accepted — correctness fix for the model Haven targets. The offline half lands
with this ADR; a live 400/no-400 confirmation is pending a paid run and is
tracked in `docs/EVAL_LIVE.md`.

## Gate: problem

DeepSeek V4 runs in thinking mode by default and returns a `reasoning_content`
field beside `content`. Its documented protocol has a rule Haven violates:

> Between two user messages, if the model performed a tool call, the
> intermediate assistant's `reasoning_content` must be passed back to the API
> in all subsequent requests. If it is not, the API returns a 400.

Haven's whole loop is tool calls, and it sends `tools` on every request, so it
hits this rule constantly. The adapter reads `reasoning_content` from the stream
but discards it after showing it to the UI: it is never captured onto the
assistant message and never re-sent. langchain, laravel/ai, and opencode all
shipped this exact bug and fixed it.

A comment in `openai_compatible.py` even asserted the opposite ("most providers
reject their own reasoning on input"). That is true for the no-tool-call case
(DeepSeek ignores it) and false for the tool-call case (DeepSeek requires it).

## Gate: options

- *Fold reasoning into the assistant `content` so it round-trips for free.*
  Rejected, hard. Reasoning is not the answer. Putting it in `content` would
  render it to the user, feed it to the Evidence Gate's review, and let it into
  the compaction digest as if it were established fact — breaking the invariant
  that has held since ADR 0001.
- *Always send `reasoning_content` back for every provider.* Rejected: a
  provider that does not expect the field may reject it. Replay must be gated on
  a declared capability.
- **Carry reasoning as an opaque, inert field on the assistant message, and
  replay it on the wire only when the provider's profile declares it required.**
  Accepted.

## Decision

- `ModelResult` and `ModelMessage` gain `provider_reasoning: str = ""`. It is
  wire-protocol baggage, nothing more: never rendered as the answer, never
  written to the evidence ledger, never entered into the compaction digest,
  never labelled trusted. `ModelMessage.content` remains the only thing that is
  any of those.
- `RunService` accumulates the streamed `ReasoningDelta` text for a turn and
  stores it on both the `ModelResult` and the assistant `ModelMessage` it
  appends to the transcript. The UI event is unchanged; reasoning is still shown
  live and still kept out of `content`.
- `ModelProfile` gains `requires_tool_call_reasoning: bool`, true for
  `deepseek-v4-flash`. Bootstrap passes it to the OpenAI-compatible adapter.
- The adapter, when the flag is set, includes `reasoning_content` on every
  assistant message that carries tool calls — the captured value, or an empty
  string when history predates the field. That empty-string back-fill is what
  the other implementations use for persisted conversations, and DeepSeek
  accepts it.
- Because `ModelMessage` is what `CheckpointV1` persists, reasoning survives a
  checkpoint/resume round trip automatically. Without this, a resumed run would
  400 on its very first turn — the worst place to discover the bug.

## What this does and does not change

- **Unchanged:** reasoning is not the answer. Every existing guarantee about
  `content`, the ledger, the digest, and trust labelling holds verbatim.
- **Unchanged:** providers that do not set the capability flag receive exactly
  what they did before — no `reasoning_content` on the wire.
- **Changed:** against DeepSeek V4, a multi-turn tool-calling run no longer
  risks a 400 on its second turn.

## Gate: metrics

- Contract tests against synthesized wire payloads: with the flag set, a
  tool-call assistant message serializes with `reasoning_content` (captured or
  empty); without the flag, it never does; a non-tool assistant turn does not
  carry it.
- A `RunService` test: streamed reasoning is captured onto the assistant
  transcript message and is absent from `content`.
- A checkpoint round-trip test: `provider_reasoning` persists and restores.
- Pending, live: whether an un-patched Haven actually 400s against the current
  API, recorded in `docs/EVAL_LIVE.md` rather than asserted here.

## Gate: risks

- **The API contract changes again.** The capability is one boolean on one
  profile; following a change is a one-line edit.
- **Reasoning bloats the transcript and the checkpoint.** It is only carried on
  assistant messages that made tool calls, and compaction still drops old *tool*
  outputs (the bulk). If it ever matters, reasoning older than the last few
  turns can be dropped from replay — DeepSeek only strictly needs the most
  recent tool-call turn's reasoning present, but sending all is simplest and
  correct.

## Rollback

Remove `provider_reasoning` from `ModelResult` / `ModelMessage`, the capture in
`RunService`, the adapter's `reasoning_content` emission, and
`requires_tool_call_reasoning` from the profile. Persisted checkpoints keep the
field harmlessly (it defaults to "").
