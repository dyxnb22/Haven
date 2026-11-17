# Model presets and usage

**English** | [中文](MODEL_PRESETS.zh-CN.md)

Find the user `config.toml` one level above the `skills.user_dir` reported by
`haven config explain`. Add a preset for the current provider to use it; `luna`
below is the preset name.

```toml
[model_presets.luna]
model = "gpt-6-luna"
```

A dual-track preset uses `fast_model` and `deep_model` instead, and may also set
`fast_turns`. Fill in model IDs offered by the current provider. The fast track
explores; the deep track is taken once an edit has happened, a plan has started
implementing, or the fast-turn count is exceeded. `fast_turns = 0` goes deep on
the first turn. A user configuration may define up to 16 presets.

```bash
haven config explain
haven run "梳理测试入口" --model-preset luna
```

`haven continue` and `haven implement` can select a preset for a new turn too.
Omitting the option uses the model or routing configuration from startup.
`--model-preset` is mutually exclusive with `--fast-model` and `--deep-model`.
Presets share the provider endpoint and key environment variable with the
default model; their definitions live in the user configuration.

The TUI's `/model` shows the configuration, the current selection, and the most
recent actual call. `/model use luna` applies the preset to runs newly started in
this TUI session, including follow-ups and plan implementation. `/model clear`
restores the startup model or routing. The command works while a run is active
or paused. Resume and answering within the same run keep the model and the
fast/deep route saved in the checkpoint, including a preset later renamed or
deleted. If the provider endpoint or user rates have changed, restore the
original settings before resuming. A paused answer is still preserved. Legacy
checkpoints without a model snapshot use the startup configuration.

## Models and cost

`haven config explain` reports configuration provenance, the context character
budget, known token windows, and reasoning settings. CLI output, the TUI trace,
and exported reports record the actual model, the reason for the selection, the
usage source, and the cost.

The character budget is Haven's limit when assembling messages. A model's
documented window and output cap are two separate capabilities.

`gpt-4o-mini` on OpenAI's official endpoint carries a built-in standard text
rate. OpenAI has published a 2027-01-14 deprecation date for that model.
[Model parameters](https://developers.openai.com/api/docs/models/gpt-4o-mini) ·
[API models and deprecations](https://developers.openai.com/api/reference/resources/responses/methods/create)

The same endpoint also supports explicitly selecting `gpt-6-luna`. Haven uses
Chat Completions, sets `reasoning_effort = "none"` for function calling, and
bounds output through `max_completion_tokens`. Luna's cache-write and
long-context tiered pricing fall outside what the built-in rate card can
express, so cost shows as unknown by default. Request and tool round-trips for
both models were validated against locally mocked responses; account
availability still depends on the actual provider.
[GPT-6 Luna model parameters](https://developers.openai.com/api/docs/models/gpt-6-luna) ·
[Chat Completions parameters](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

Built-in public rates are matched by official endpoint and the model actually
called. Compatible gateways supply their own rates, which can be given in the
user configuration through the existing `[pricing]` table. That user rate card
overrides every model in this configuration.

Cost is estimated as usage from completed model calls multiplied by the rate,
and reports mark it `estimated`. When a provider returns no token counts, Haven
estimates them from the request and output and labels the usage source. If any
call lacks a rate, the run total shows `unknown`. The event and JSON
`cost_usd` keep the priceable portion, and `cost_known` records whether the whole
run can be estimated. Cost budgets compare against the priceable portion; step,
time, and token limits apply as usual.

## Common behaviours

| Situation | What Haven does |
|---|---|
| Authentication failure | 401 and 403 end the run, the event is marked `auth`, and the response body is hidden |
| Rate limiting | Bounded backoff that honours `Retry-After`, at most two retries |
| Transient network failures and 5xx | Bounded retries on the same model; if fragments were already emitted, the event marks a stream restart |
| Output truncation | Up to two continuations. Models needing native prefix must also match the endpoint and routing candidates; the rest use conversational continuation |
| Tool calls | Assembles streamed tool arguments and maps them back to tool names, then executes through the existing approval and evidence flow |
| Tool reasoning replay | Replayed per model configuration. If the first request explicitly reports the relevant 400 error before the stream starts, the adapter retries once |
| Context overflow | Narrows the character budget for the model actually used only, at most two retries per step |
