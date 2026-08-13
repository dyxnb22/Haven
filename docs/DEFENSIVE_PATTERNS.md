# Defensive patterns

Bug-class rules. Each one is a defect that actually shipped or nearly shipped in
this repository, restated as the rule that prevents its recurrence. The
narrative versions live in [`POSTMORTEM.md`](POSTMORTEM.md) and the ADRs; this
page is the checklist to read *before* writing policy, boundary, gate, or
provider code.

## Question the measurement before the finding

A safety metric that fires is first a claim about the detector. The offline
suite's first real run reported four unauthorized file changes; the detector was
literally correct (those bytes moved) and the specification was wrong — derived
`__pycache__` bytecode from an approved check is not an agent mutation. A gate
that cries wolf is worse than no gate, because it teaches you to skim red output.
Establish which of the two is broken before you either celebrate a catch or
suppress it.

## One global, explainable exclusion beats N local exceptions

The tempting fix above was adding `.pyc` paths to each failing case's
allow-list: two minutes, and it converts a tight per-case allow-list into one
carrying interpreter-version noise that every future case must repeat. Every
widened allow-list is somewhere a real violation can hide. Fix the definition
once, centrally, with the reason written next to it.

## A gate needs positive assertions, not just absences

"Nothing bad changed" is satisfied by an agent that did nothing at all. Pair
every absence check with a specific positive one — the security cases assert the
exact `policy.decided` deny reason occurred, so they cannot pass vacuously.

## Provenance travels with the data; never infer it from position

`ContextBuilder` once classified segments by list index (`index == 0` →
`system_rules`, trusted), so repository-authored `AGENTS.md` text was reported as
trusted while sitting in the system role. Position-derived metadata is correct
until the list changes. Carry an explicit record (`_Selected(message, source,
trust, reason)`) from selection through truncation so a label cannot drift from
its content. And prefer structure over prose: "the sentence says untrusted" is
weaker than "the role, the type, and the trace say untrusted".

## For "must not reach X", enumerate what is allowed

`conftest.py` stripped two named credential variables, which is an allow-list of
the ones someone thought of; a `DEEPSEEK_API_KEY` in the shell let the "offline"
suite spend real money. Deny-lists are always missing the one that matters.
Enumerate the permitted set (`ENV_ALLOWLIST` for spawned processes) or match a
shape (`_API_KEY`/`_KEY`/`_TOKEN`/`_SECRET`), then **guard the guard** with a
test asserting the invariant itself holds.

## Catch I/O by category, not by one library's exception tree

A TLS record-layer failure is an `ssl.SSLError`, not an `httpx.HTTPError`, so it
escaped every `except ProviderError`, sailed past the retry loop, and killed a
31-case live suite mid-run. Translate failures at the boundary by what they *are*
(an `OSError` is I/O) rather than by the type one dependency happens to raise —
and keep `except Exception` out of it, so a bug in our own parsing still surfaces
as itself instead of being retried as a network blip.

## Repair structural invariants at the one boundary everything passes

Compaction and crash recovery can each leave a tool-call/tool-result pairing
locally broken, and a strict provider 400s on it. Rather than trust every
upstream path to maintain the invariant, `_sanitize_history` enforces it
deterministically at the wire boundary — the single place every request goes
through. Prefer one chokepoint that repairs to N callers that must remember.

## Distinguish recoverable failures from terminal ones, and say which

Everything that was not a transport blip failed the run, so a provider 400 that
merely meant "this prompt is too long" — a situation the provider had just
diagnosed precisely — discarded the whole run. Give a recoverable failure its own
code and an actual recovery path (ADR 0027). The inverse matters too: an
out-of-credit 402 reported as a generic `protocol` error reads to a user like a
bug in our own request.

## A total deadline is not a liveness check

Capping a whole model response at 120 seconds killed healthy generations: a
reasoning model streams for minutes, and that stream is alive, not stalled. Bound
the *gap between* events and reset it on each one; let a separate, explicit
budget bound the total. The failure mode of confusing the two is paying twice for
the same long think after the "timeout" retries.

## Widening a return type silently rewrites every call site

Changing `StuckLoopDetector.observe` from `bool` to a `"ok" | "nudge" | "stuck"`
verdict left `if stuck.observe(fp):` compiling and type-checking — and truthy for
*every* outcome, so each tool call looked stuck. Only unrelated tests
(step-budget, provider-error) failing revealed it. When a return type widens,
audit call sites for truthiness rather than trusting the type checker, and keep
tests that pin the *other* branches.

## A generated number must fail loudly when its inputs are stale

`refresh_metrics.py` reports coverage from whatever `.coverage` already holds; it
never re-runs the suite. Refreshing after editing source but before re-running
coverage published a wrong figure (89% → "84%") into a table CI then enforces.
Anything generated from a cached artifact needs a freshness check that fails
rather than a number that quietly lies.

## State the premise a security argument rests on, and re-check it

`repo.exec`'s auto-allow was justified by "capability is bounded by the OS
sandbox, so a misclassification costs a skipped prompt, not an escape". True for
writes; false for reads, because the sandbox deliberately leaves non-`$HOME`
paths readable and exec output is returned to the model (ADR 0026). Write the
premise down where the code enforces it — that is what makes it auditable later,
and it is what let a review find the gap.
