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
the run.

## Measured (2026-08-14): kept

The A/B this note demanded has been run. Seven tier-3 cases × three repetitions
× two arms, n=21 per arm, differing only in the case-JSON field `repeat_nudge`.
Full numbers and failure attribution are in `docs/EVAL_LIVE.md`; the decision
rule was fixed before the run.

| Arm | n | median steps | mean steps | worst run | passed |
|---|---|---|---|---|---|
| control (nudge off) | 21 | 11 | 13.8 | 24 | 19/21 |
| treatment (nudge on) | 21 | 11 | 11.2 | 18 | **21/21** |

Mean paired delta **−2.9 steps**, and the treatment passed more cases. Both
"keep" conditions were met, so the nudge stays. Three things keep this from
being overstated:

- **The medians are identical (11 vs 11).** The nudge does not make a
  converging run faster; it truncates the tail. Control's worst runs were 24,
  24, 21, 21, 20; the treatment's worst was 18. That is the mechanism's
  predicted signature, which is why it is more convincing than the mean.
- **The point estimate leans on one case.** Drop `t3-rich-truncate-ellipsis`
  and the mean delta falls to −1.3, inside the "inconclusive" band. An exact
  paired permutation test over the seven per-case deltas gives p = 0.125
  one-sided — not significant, and at seven cases it could not have been.
- **Both control failures were that same case**, at 24 steps
  (`step_budget_exhausted`) and 21 steps (`evidence_missing`); all three
  treatment runs of it finished in 9–11 steps and passed.

Kept, therefore, on a real but modest and imprecisely-located effect. The
supporting number that needs no significance test: the control arm spent
3.71M input tokens against the treatment's 2.34M for the same work.
