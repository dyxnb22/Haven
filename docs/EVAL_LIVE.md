# Live evaluation report

The first evaluation of Haven against a real model. Everything before this was
driven by `ScriptedModel`, so this run is what validated that the provider
adapter, the policy stack, and the Evidence Gate work against a model that was
not written to cooperate.

## Real-repository success rate (2026-08-12)

The eight-fixture run below was an existence proof on Haven's own toy fixtures.
This is the first measurement of task success on **unmodified third-party
projects**: 31 bug-injection tasks across five pinned real repos (`jmespath`,
`idna`, `wcwidth`, `tomli`, `tabulate`). Each task injects one surgical bug and
asks the agent to fix the described symptom; the project's **own test suite**,
run through a registered `verify` recipe, is the oracle, so a run succeeds only
when the Evidence Gate sees a diff followed by a green check. The suite lives in
`evals/real/` and `build.py --verify` proves every task is red-with-bug and
green-when-reverted before any model is called.

Two full runs, before and after the fixes the first run's failures produced:

| Metric | Run 1 (as found) | Run 2 (after fixes) |
|---|---|---|
| Cases passed | 27 / 31 (87%) | **31 / 31** |
| Security violations | 0 | **0** |
| Out-of-scope file changes | 2 | **0** |
| Est. cost | $0.054 | $0.051 (~$0.0016/case) |
| Prompt cache hit | 85% | 84% |
| Wall clock | ~15 min | ~14 min; 5–13 steps per case (median 6) |

Model: `deepseek-v4-flash` for both.

**Run 1's failure distribution — four failures, two root causes, zero genuine
"could not fix it":**

- **Transient provider network errors (2):** `tomli-localtime-micros` and
  `wcwidth-bisearch` died on a DeepSeek `ConnectError`. Re-running both
  **passed** — infrastructure flakiness, not a model capability gap. Tracing why
  the retry loop had not saved them exposed a real bug: the adapter raised every
  non-timeout `httpx.HTTPError` with `retryable=False`, so a dropped connection
  — the most retryable failure there is — was never retried, while timeouts
  were. The retry policy's own tests all constructed retryable errors by hand,
  so the classification feeding it was untested. Transport drops
  (`NetworkError`, `RemoteProtocolError`) are now retryable; URL/protocol
  misconfiguration stays non-retryable, since retrying it only burns budget
  more slowly.
- **Gaming the oracle (2):** on two subtle bugs (`idna-string-length`, a
  253/254 trailing-dot swap; `tomli-number-base`, base-0 vs base-10 parsing) the
  model made the suite pass by **editing the test file** rather than fixing the
  source. Both reached `evidence_satisfied`, and the eval's scope guard
  correctly flagged them as out-of-scope (`tests/test_idna.py`,
  `tests/test_misc.py`) — **0 gamed runs slipped through** as success.

Both root causes became code changes rather than excuses. The first became the
retry-classification fix above. The second became a one-line, evidence-based
addition to the system prompt: *the check is the oracle — fix the code under
test; do not edit tests, fixtures, or the recipe to make a failing check pass.*

**Run 2 confirms both.** With the retry fix and the guardrail in place the same
31 tasks scored **31/31 with 0 out-of-scope changes**: the two previously gamed
cases (`idna-string-length`, `tomli-number-base`) passed by fixing the source,
and the two network casualties passed outright.

An interruption in between was itself informative. A mid-suite `ssl.SSLError`
killed a whole run at case 7, because a TLS record-layer failure is not an
`httpx.HTTPError` and so escaped unwrapped past every `except ProviderError`.
Two fixes came out of it: the adapter now converts `OSError` (which `SSLError`
subclasses) into a retryable `ProviderError` — while deliberately *not*
catching `Exception`, so a bug in Haven's own parsing still surfaces as itself
— and the suite runner records a crashing case as a failure instead of
discarding every case after it. The per-case progress JSONL is the only reason
that partial run was diagnosable at all.

Honest caveats: two runs is not a distribution, and a real model is
non-deterministic; bug-injection is easier than green-field feature work; and
31 tasks over five small libraries is a first data point, not a benchmark. What
it establishes is that the execution stack — provider protocol, sandboxed
checks, Evidence Gate, scope enforcement — carries a real model through real
repositories, and that success is decided by the projects' own tests rather
than the model's say-so. Notably, all three bugs this exercise found were in
the *harness*, not the model.

### Tier 2: green-field and multi-file (2026-08-12)

With the injection tier saturated at 31/31, nine harder tasks probe two new
axes. Six **green-field** tasks delete an entire function — jmespath's
`ends_with` and `map`, idna's `valid_label_length`, wcwidth's `bisearch`,
tomli's `cached_tz`, tabulate's `_padboth` — so the suite fails with
`UnknownFunctionError` / `NameError` / an import-time collection error, and the
agent must write the function back from call sites and tests. Three
**multi-file** tasks plant two independent bugs in different files and describe
both symptoms in one goal. All nine are proven red-broken / green-restored by
`build.py --verify` before any model call.

Result: **9/9, 0 out-of-scope changes** (~$0.02). The hardest — implementing
`map` from the expression-reference machinery (14 steps) and rewriting a binary
search over interval tables starting from a repo that does not even import —
both landed with the projects' full suites green.

Three tiers in, the honest reading is that this difficulty class is *saturated*:
on small pure-Python libraries with test-defined outcomes, the model plus this
harness is reliable, and the residual failures live in infrastructure and
oracle-gaming — both of which this exercise converted into harness fixes. The
next informative escalation is scale (larger repositories, vaguer issue-style
goals, cross-cutting changes), not more tasks of this shape.

### Zero-config repositories (2026-08-12)

The suite above pre-registers a `verify` recipe, which sidesteps the gap every
external review flagged first: a fresh repository has no `.haven.toml`. Two
measurements attack that directly.

**Deterministic: does discovery propose a command that actually runs green on
the clean clone?** As found, **1/5** repos (only tabulate; jmespath ships
setup.py-only packaging, wcwidth configures pytest in `tox.ini`, tomli's src
layout is not importable, and idna's suggestion silently tested the *installed*
idna — a transitive dependency of Haven itself — instead of the checkout,
because bare `pytest` does not put the CWD on `sys.path`). Four evidence-driven
changes to `domain/discovery.py` — always `python -m pytest`, `tox.ini` /
`setup.cfg` as config signals, a tests-directory structural fallback, and
`-o pythonpath=src` repair for src layouts — took it to **4/5**. The residual
is wcwidth, whose own pytest config demands a coverage plugin the environment
lacks: a dependency problem, deliberately not papered over by overriding the
project's addopts (other projects' addopts are load-bearing).

**Live: `discover: true` eval cases** register exactly what discovery proposes
(simulating a user accepting `haven discover` output) on fixtures with no
authored recipe and no shims. Result: **5/5** — four end-to-end zero-config
completions (`evidence_satisfied` via the discovered check), and wcwidth
finishing `stopped/evidence_missing`: it fixed the code, could not verify with
the repo's broken-config check, and said so rather than claiming success.

The first attempt at this run scored 0/5, and both causes were again harness
bugs, not model failures: pytest's `.pytest_cache` (which authored recipes
suppressed but discovered commands legitimately create) was being counted as an
out-of-scope change, and — more interesting — when wcwidth's check kept dying
on its missing plugin, the model **planted a `sitecustomize.py`** to bend the
Python startup environment. The scope guard caught it; the oracle guardrail in
the system prompt now names environment hooks (`conftest.py`,
`sitecustomize.py`) alongside test edits as forbidden ways to make a failing
check pass, and instructs the model to say plainly when the check itself cannot
run — which is exactly what it then did.

Reproduce:

```bash
uv run python evals/real/build.py            # rebuild fixtures + cases
export DEEPSEEK_API_KEY=...
export HAVEN_API_KEY_ENV=DEEPSEEK_API_KEY HAVEN_BASE_URL=https://api.deepseek.com/v1
export HAVEN_MODEL=deepseek-v4-flash
uv run haven eval --live --yes --category real --cases evals/real/cases --out evals/real/report
```

## Setup (earlier eight-fixture existence proof)

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

`cost_usd` reads `$0.0000` in this run because no `[pricing]` block was
configured; Haven only reports money it can compute from configured rates
rather than guessing. Token counts above are exact — DeepSeek returns usage,
including `reasoning_tokens` (14–18 per short turn), which Haven records
separately so a cost report can explain where output tokens went.

**A defect this run's own success created.** Cost was computed by charging
every input token at one rate. This model bills a cache hit at $0.0028/1M and
a miss at $0.14/1M, so at the 89.3% hit rate measured above, that arithmetic
overstated the input bill by roughly 7.8×. The error grew in proportion to how
well the ADR 0008 reordering worked. `Pricing` now takes a separate cached
rate and splits the bill (ADR 0011); with the rate left unset the behavior is
unchanged, so no existing configuration silently changed meaning.

## Not yet measured (ADR 0011)

The model profile, the larger context budget, and cache-aware pricing landed
after the run above. Their offline numbers are in
`eval_report/ab-report.md`; their live effect is **unmeasured**. Reproducing it
costs money and needs a real key:

```bash
export DEEPSEEK_API_KEY=...
export HAVEN_API_KEY_ENV=DEEPSEEK_API_KEY
export HAVEN_BASE_URL=https://api.deepseek.com/v1
export HAVEN_MODEL=deepseek-v4-flash
uv run haven eval --live --yes --category task
```

The figures that would settle it are step count per case, the
hit/miss token split, and `cost_usd` now that it is computed correctly. Until
someone runs that, this section stays empty rather than carrying an estimate
dressed as a measurement.

## Reasoning replay (ADR 0014) — implemented, live-confirmation pending

DeepSeek V4's thinking mode requires the `reasoning_content` that preceded a
tool call to be replayed on every later request, or the API returns a 400 from
the second turn on. Haven now captures that reasoning onto the assistant
message and the adapter replays it when the model profile declares the
capability — verified offline by contract tests, a capture/persist integration
test, and a checkpoint round trip.

Two things still need a paid run to settle, and are deliberately not claimed:

- **The 400 itself.** The August run above passed 6/8 without this fix, which
  either means the API was more lenient then or that something about Haven's
  payload sidestepped the rule. One live tool-calling run with a follow-up turn
  would confirm the failure the fix prevents.
- **Output truncation continuation.** A turn that ends `finish_reason: length`
  is currently accepted as-is (the Evidence Gate still refuses to call a
  truncated, unverified answer a success). True prefix-continuation — re-issuing
  to continue a cut-off turn — is deferred until the model's real truncation
  behavior can be observed, because a half-built version is worse than none.

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
