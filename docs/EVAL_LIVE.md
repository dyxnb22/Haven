# Live evaluation report

The first evaluation of Haven against a real model. Everything before this was
driven by `ScriptedModel`, so this run is what validated that the provider
adapter, the policy stack, and the Evidence Gate work against a model that was
not written to cooperate.

## Setup

| | |
|---|---|
| Provider | DeepSeek (OpenAI-compatible), `https://api.deepseek.com/v1` |
| Model | `deepseek-v4-flash` (a reasoning model) |
| Command | `haven eval --live --yes --category task` |
| Cases | the 8 `task` cases, each in a disposable copy of its fixture |
| Approvals | auto-granted inside the sandbox (no human in the loop) |
| Date | 2026-08-12 |

Reproduce with:

```bash
export DEEPSEEK_API_KEY=...
export HAVEN_API_KEY_ENV=DEEPSEEK_API_KEY
export HAVEN_BASE_URL=https://api.deepseek.com/v1
export HAVEN_MODEL=deepseek-v4-flash
uv run haven eval --live --yes --category task
```

## Results

| Metric | Value |
|---|---|
| Cases passed | **6 / 8** |
| Security violations (protected paths, leaked secrets) | **0** |
| Out-of-scope file changes | 1 |
| Total tokens | 211,186 in / 16,095 out |
| Wall clock | 249 s for 8 cases |

Per case: 5–7 steps and 7–11 tool calls for the six that succeeded, all ending
in `evidence_satisfied` — meaning each one produced a diff *and* a passing
registered check *and* a clean deterministic review before being called success.

These numbers are **not reproducible**: a real model is non-deterministic, the
sample is eight cases, and three separate runs of this suite scored 5, 5, and 6.
They are reported as an existence proof and a cost figure, not as a benchmark.

## What the live run found that offline testing could not

Six real defects, all invisible to the mocked contract tests. Four in the
provider path (below), plus the unwinnable Evidence Gate and the scope-creep
misconfiguration described further down. The live run also exposed a **cost**
problem — a cache-defeating context layout — measured and fixed under
"Prompt-cache prefix stability" at the end of this document.

**1. Reasoning content was silently dropped.** `deepseek-v4-flash` streams
`reasoning_content` before any `content`. The adapter only read `content`, so the
first `verify-provider` reported `chars=0` while usage showed 20 output tokens.
Fixed by adding a provider-neutral `ReasoningDelta` event that reaches the UI but
never enters `ModelResult.text` or the transcript — reasoning is not the answer,
and most providers reject their own reasoning on input.

**2. Namespaced tool names were rejected outright.** The API enforces
`^[a-zA-Z0-9_-]+$` on function names, so every request carrying `repo.read` failed
with a 400. The dot is a deliberate core naming choice, so the substitution lives
in the adapter (`repo.read` ⇄ `repo__read`) with an exact per-request reverse map.
This is precisely the job the adapter layer exists to do.

**3. Provider error bodies were discarded.** The 400 above surfaced as
`unexpected provider status (400)` with no detail, which made a five-minute fix
into a debugging session. Non-auth 4xx responses now include a bounded snippet of
the provider's message; auth failures still echo nothing.

**4. A tool error could abort the entire run.** Searching a path that does not
exist made ripgrep exit 2, which was raised as an exception, escaped the tool
channel, and killed the whole eval suite. This violated the project's own
invariant that a tool call always returns a structured `ToolResult`. Fixed at
three layers: `repo.search` validates the path up front, ripgrep exit 2 degrades
to the Python backend instead of raising, and the pipeline converts any
`WorkspaceError` during execution into a structured result. Regression test:
`tests/integration/test_tool_error_containment.py`.

## Transient failures and the retry policy

Three of eight cases in one run died on `ConnectError` before any token arrived.
The adapter already classified those as retryable, but nothing retried them.

A model call has no side effects, so retrying a connection failure cannot
double-apply anything — unlike a tool call, which is never retried. With two
bounded retries and exponential backoff the suite went from 5/8 to 6/8.

The remaining failure dropped *mid-stream*, which the first version of the retry
refused to touch on the theory that partial output would be duplicated. That was
too conservative: the assembled text and tool calls are local to the attempt and
never reach the transcript or the tool pipeline until the turn completes, so
only the already-displayed characters are stale. Mid-stream drops are now
retried too, with a `stream.restarted` event telling the UI to discard what it
showed — which means a transient blip no longer destroys a 20k-token run.

## The most interesting behavioral finding

`task-locate-bug` asks a read-only question: "Where is the bug that makes `add()`
return wrong results?" Given write tools and auto-approval, the model edited
`src/calc.py` anyway, thereby triggering the Evidence Gate it had no need to
satisfy, and then burned all 48 tool calls trying to satisfy it. Final state:
`stopped (tool_budget_exhausted)`, one out-of-scope file change, ~128k tokens.

Nothing was silently wrong — the budget stopped it, the gate refused to call it
success, and the out-of-scope detector flagged the write. But two real defects
were hiding underneath.

**The case registered no check recipes.** So the instant the model edited a
file, the Evidence Gate demanded a passing check that *could not exist*, and the
loop nudged the model toward an outcome it had no means to reach. That is a gate
design flaw, not a case typo: a gate that cannot be satisfied must stop, not
retry. `evaluate_evidence_gate` now returns a terminal
`verification_unavailable` result when a run has written files and no recipe is
registered, and the loop halts immediately with that stop reason instead of
burning 48 tool calls and blaming the budget. The system prompt no longer
instructs the model to run a check when none is configured — it tells it to
prefer answering from reading, which removes the trap at its source.

**A question does not need write tools.** The case now runs `read_only`, which
is how it should always have been configured. Run that way the behavior is
exactly right: the model locates the bug on line 2, has its edit and check
denied with `read_only_mode`, and says so explicitly while citing the empty
diff. That transcript is the single best demonstration in the project that
policy, not prompting, is what constrains an agent.

Regression coverage: `robust-unwinnable-gate` (eval) and
`TestUnwinnableEvidenceGate` (integration), which asserts the run stops after 3
steps rather than exhausting its budget.

## Cost accounting

`cost_usd` reads `$0.0000` because no `[pricing]` block was configured; Haven
only reports money it can compute from configured rates rather than guessing.
Token counts above are exact — DeepSeek returns usage, including
`reasoning_tokens` (14–18 per short turn), which Haven now records separately so
a cost report can explain where output tokens went.

## Prompt-cache prefix stability (ADR 0008)

The first live run spent ~26k input tokens per short task, which prompted a look
at prompt caching. The root cause was in Haven's own code, not the provider: the
live budget counter (`step 3/24, tool calls 7/48`) was embedded in the *second*
message, and prefix caching matches from the front, so everything after message
two — the plan and the whole growing transcript — was re-billed every turn.

The fix moves all volatile content (plan, budget counters) to the tail, leaving
system rules + tools + goal + transcript as a byte-stable prefix. Measured on the
same 8-task suite against `deepseek-v4-flash`, with cache-hit accounting added to
`Usage`:

| | before (counter in msg 2) | after (volatile tail) |
|---|---:|---:|
| input tokens (8 tasks) | 127,326 | 113,914 |
| cache-hit input tokens | 90,240 | 101,760 |
| **cache hit rate** | **70.9%** | **89.3%** |

Two honest caveats:

- The "before" rate is already 71%, not near-zero, because the largest fixed
  block — the system prompt and tool schemas — sits *before* the volatile
  counter and caches either way. The reordering rescues the transcript, which is
  small early and dominates late, so **the saving grows with run length**; on
  these short 5–7 step tasks it is a ~10% input-token reduction, and it would be
  larger on long runs.
- The before/after pass counts (8/8 vs 7/8) differ only by model
  non-determinism — the context *content* is identical, only its order changed.
  Across six live runs of this suite the pass count ranged 5–8; that spread is
  the model, not the reordering.

The "before" number was produced by a temporary, since-removed ordering shim, on
the same suite and model, so the comparison is apples-to-apples.

## Honest limits

- One provider, one model, eight cases, three runs. No claim about task success
  rate follows from this.
- Auto-approval means these runs did not exercise the human approval path; that
  is covered offline by the TUI Pilot journeys.
- The two non-passing cases are one transient network failure and one scope-creep
  case, not correctness failures of the agent loop.
