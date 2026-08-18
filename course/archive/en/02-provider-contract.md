# Module 02 — The provider contract and streaming

**English** | [中文](../../02-provider-contract.md)

> Files: `src/haven/contracts/model.py`, `src/haven/ports/model.py`,
> `src/haven/adapters/providers/openai_compatible.py`,
> `src/haven/adapters/providers/scripted.py`
> Tests: `tests/contract/test_openai_compatible.py`, `tests/contract/test_scripted_model.py`
> ADR: [0001 — language and scope](../../../docs/adr/0001-language-and-scope.md)

## Learning objectives

- Design a **provider-neutral** model contract that keeps wire-format details out
  of your core.
- Stream events (text, tool calls, usage, finish) with timeouts, size limits, and
  cancellation.
- Build a **scripted** model so your whole system is testable offline and
  deterministically.
- Understand why a fake is not a shortcut but a load-bearing design tool.

## The concept

Your agent should not know which provider it is talking to. If OpenAI-specific
field names leak into your loop, you cannot swap providers, you cannot test
offline, and provider quirks become core bugs.

Haven draws the line with three pieces:

- **A neutral contract** (`contracts/model.py`): `ModelRequest`, a discriminated
  union of `ModelEvent`s (`TextDelta`, `ReasoningDelta`, `ToolCallReady`,
  `UsageReport`, `StreamFinished`), and an assembled `ModelResult`. Nothing here
  says "OpenAI."
- **A port** (`ports/model.py`): `ModelPort` is a `Protocol` with
  `generate_stream(request) -> AsyncIterator[ModelEvent]`, plus a stable
  `ProviderError` with a small code enum. The core depends only on this.
- **Adapters** (`adapters/providers/`): the OpenAI-compatible adapter is the
  *only* place that knows about SSE, `choices[].delta`, or DeepSeek's cache
  fields. `ScriptedModel` implements the same port by replaying authored events.

## Streaming is where the hazards live

Read `_stream` in `openai_compatible.py`. Notice what it does beyond parsing:

- **First-event timeout vs. total timeout.** A provider that accepts your
  request then goes silent must not hang the run forever. The first token has a
  tighter deadline than the whole stream.
- **Response size cap.** A misbehaving stream cannot exhaust memory.
- **Stable error mapping.** HTTP 401/403 → `auth`, 429 → `rate_limited`, 5xx →
  `server`, malformed chunk → `protocol`. The core never sees a raw provider
  body — except deliberately: a non-auth 4xx includes a bounded snippet, because
  that is almost always *your* malformed request and the message is how you fix
  it. Auth failures echo nothing.
- **The API key never appears** in errors, logs, or traces. A test asserts this.

## The fake is the default, not the fallback

`ScriptedModel` replays a list of turns, each a list of `ModelEvent`s, from
plain JSON. This is why the entire test suite and the whole offline eval are
deterministic and free. Read `tests/contract/test_scripted_model.py`: it shows a
turn being authored and replayed. The lesson: **decide early that your fake is a
first-class citizen.** If you bolt it on later, your core will have grown
provider assumptions that make faking impossible.

## Two quirks the mocks could not have taught you

Both were found only by pointing Haven at a real reasoning model (DeepSeek), and
both are documented in `docs/EVAL_LIVE.md`:

1. **Hidden reasoning.** The model streamed `reasoning_content` before any
   answer `content`. A naive adapter drops it and reports "0 characters" while
   billing output tokens. Haven surfaces it as a distinct `ReasoningDelta` that
   reaches the UI but never enters `ModelResult.text` or the transcript —
   reasoning is not the answer, and most providers reject their own reasoning on
   input.
2. **Namespaced tool names rejected.** The API enforces `^[a-zA-Z0-9_-]+$` on
   function names, so `repo.read` 400s. The dot is a core naming choice, so the
   substitution (`repo.read` ⇄ `repo__read`, with an exact per-request reverse
   map) lives in the adapter. This is the adapter layer doing exactly its job:
   absorbing a wire quirk so the core keeps its clean vocabulary.

## Exercises

1. **Trace an event.** In `openai_compatible.py`, follow a single SSE line from
   bytes to a `ModelEvent`. Where would you add support for a provider that
   sends tool-call arguments as a single blob instead of deltas?
2. **Author a turn.** Write a JSON turn for `ScriptedModel` that emits some text,
   then a `repo.read` tool call, then finishes. Run it through the pattern in
   `tests/contract/test_scripted_model.py`.
3. **Add a failure test.** Using `respx`/`httpx.MockTransport` as in
   `tests/contract/test_openai_compatible.py`, assert that a 429 maps to
   `rate_limited` and that a made-up API key never appears in the raised error.
4. **(Live, optional)** Point Haven at any OpenAI-compatible endpoint and run
   `uv run haven verify-provider --yes`. Read the output: TTFT, usage, and — for
   a reasoning model — the reasoning-vs-answer character split.

## Self-check

- Why is `ModelPort` owned by the core rather than imported from an adapter?
- A colleague wants to "just pass the OpenAI response object around, it's
  simpler." Give the concrete cost in terms of testing and provider swap.
- Why does a mid-stream failure need different handling from a failure before the
  first event? (Module 05 makes this precise with the retry policy.)

## Further reading

- ADR 0001 for the stack and single-provider-plus-fake decision.
- ADR 0008 and Module 09 for how the adapter learned to report cache hits.
- Commit `3727fb0` (`feat(providers)`) is the whole slice in isolation.
