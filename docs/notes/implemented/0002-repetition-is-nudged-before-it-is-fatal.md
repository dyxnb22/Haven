# Repetition is nudged before it is fatal

Date: 2026-08-13

## Context

`StuckLoopDetector` stopped a run after three identical (tool, args, result)
observations and said nothing before that. ADR 0023 attributed the dominant
live failure class to the model not converging in time, and by the point a run
is killed for repetition its remaining budget is already spent — the harness
had information the model could have used and withheld it until it was too late
to act on.

## Decision

`observe()` returns `ok | nudge | stuck`. The second identical observation
returns `nudge`, which the loop turns into one program-written transcript
message telling the model the call produced nothing new; the third still stops
the run. The note fires once per episode and any different observation resets
the count, so a later repetition is a fresh episode with its own warning.

The message names only the tool. It lands as a `user`-role message, which
`ContextBuilder` labels **trusted**, so echoing the model's own argument JSON
into it would smuggle untrusted text into trusted context — a test pins that it
does not.

## Alternatives considered

- **Lower the stop threshold to 2.** Cheaper, and strictly worse: it converts a
  recoverable situation into a dead run without ever telling the model why.
- **Emit only a UI notice.** The user sees it; the model, which is the thing
  that must change behaviour, does not.
- **Include the repeated arguments in the note** so the model sees exactly what
  it repeated. Rejected on trust grounds above; the tool name plus "identical
  arguments" is enough to identify the call the model just made.
- **Let a model-driven reviewer decide the run is stuck.** ADR 0007's benefit
  gate for a second model still applies, and a program can detect an identical
  fingerprint with no tokens and no false negatives.

## Consequences

A repetition episode now costs one extra transcript message before it can stop
the run. The mechanism is tested offline, but its *effect* is unmeasured: ADR
0023 asks for a before/after eval on the tier-3 cases that died
`token_budget_exhausted`, and that measurement has not been run. Until it is,
this is an implemented mechanism with an unproven benefit — if the A/B shows no
improvement, the honest move is to delete it rather than keep it on plausibility.
