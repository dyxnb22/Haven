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

> **Status: rejected and removed from the code (2026-08-14).** Built on the
> assumption that non-convergence looks like repetition. A trace study of 42
> live runs showed it does not, and the mechanism never fired once. The design
> and the reasoning stay here so the same idea is not re-proposed without new
> evidence; the sections below are in the order they were learned.

## REMOVED (2026-08-14): the trace study that settled it

The pre-registered gate below — *"if a trace study of the slow runs finds no
literal-repetition episodes either, the nudge and the three-strike stop are both
deleted together"* — has been run. `evals/trace_study.py` over the 42 journals:

| Cohort | runs | median calls | consecutive identical | runs with any repeat |
|---|---|---|---|---|
| slow (≥15 steps) | 11 | 21 | **0** | **1/11** |
| fast (≤11 steps) | 27 | 10 | **0** | 1/27 |

Repetition is not the signature of a slow run. It appears in 1 of 11 of them, at
a rate the fast cohort matches — so a repetition detector, at *any* window
width, cannot be the answer to non-convergence. Widening the adjacency
requirement was the obvious next move and the data rules it out too.

**What the slow runs actually do is explore without converging:**

| Tool | slow per run | fast per run | ratio |
|---|---|---|---|
| `repo.exec` | 2.1 | 0.1 | **28×** |
| `repo.read` | 7.5 | 2.4 | 3.1× |
| `repo.search` | 5.7 | 2.6 | 2.2× |
| `repo.edit` | 1.0 | 1.1 | 0.9× |

The editing rate is flat; everything upstream of it multiplies. A non-converging
run is one that keeps *looking* — and `repo.exec`, the tool for running things to
find out, is the sharpest marker.

**Decision: the nudge is deleted.** Its justification was that it addressed the
dominant failure class; that justification is falsified. `StuckLoopDetector`
returns to a two-state check and keeps its three-strike stop — that is a
pre-existing backstop against literal thrash, was not the thing under test, and
costs nothing idle. Its measured limit (an alternating A, B, A pattern never
trips it) is now pinned by a test rather than left implied.

**What the data supports building instead**, pre-registered here rather than
built on the same enthusiasm that produced the nudge: a *progress-free stretch*
signal — consecutive tool calls with no edit, check, or diff among them. It is
program-decidable, needs no model, and matches the measured shape. The gate was:
it must separate the two cohorts on journals already on disk, offline and free.

**The gate is met, and it held out of sample.** On the 42 A/B journals the max
progress-free stretch has a median of 18 in slow runs against 7 in fast ones.
Threshold 12 was then fixed and checked against **102 previously unseen runs**
from the eleven older report directories:

| Cohort | runs | median stretch | fires at ≥12 |
|---|---|---|---|
| slow (≥15 steps) | 22 | 28 | **21/22** |
| fast (≤11 steps) | 77 | 6 | 7/77 |

95% of non-converging runs, 9% of converging ones, on data the threshold was not
chosen from — against the nudge's 0 firings in 42. Honest caveat: the threshold
was scanned (6/8/10/12) on the A/B cohort before being fixed, which is why the
held-out check is the number that counts, and why the training figures are
reported beside it rather than instead of it.

This is now a *candidate*, not a decision. What it detects is a correlate of
slowness, and the obvious failure mode is that a legitimately exploratory task
trips it — 7 of 77 converging runs already do. Before anything ships, the open
question is what a detector should *do*: warning the model is the move that just
failed, and stopping a run on a correlate would convert 9% of good runs into
false stops. The likely first use is reporting, not intervention.

## WITHDRAWN (2026-08-14): the A/B measured nothing

**The nudge fired zero times in all 42 runs.** Counting `notice` events across
every run journal in the six report directories: `nudges fired = 0,
stuck-stops = 0`. The journals are complete — they carry other notices, such as
a network retry — so this is absence, not missing data.

Both arms therefore executed an identical code path, and every difference below
is run-to-run variance. The step delta, the 21/21 against 19/21, and the token
gap are all real measurements *of nothing in particular*. The pre-registered
rule was applied faithfully to data that could not speak to the intervention,
which is a failure mode the rule did not anticipate: it had three outcomes
(keep, inconclusive, delete) and needed a fourth — **invalid instrument**.

The verdict is withdrawn. The nudge returns to **implemented and untested**.

### What the run *did* establish, which is more useful

The trigger is two consecutive identical `(tool, arguments, result)`
fingerprints. In 42 real tier-3 runs — including seven that exceeded 20 steps
and two that died on budget — that pattern **never occurred once**. Neither did
the three-strike `stuck` stop, which predates this note and ships as a safety
feature.

So the dominant failure mode (ADR 0023's budget-tail non-convergence) is **not
literal repetition**. A run that burns 24 steps is issuing *different*
unproductive calls, not the same one twice. Repetition-based detection, both
the nudge and the pre-existing stop, does not engage with it.

That reframes the open question. It is no longer "does warning about repetition
help?" but "what is the actual signature of a non-converging run, and is it
detectable by a program?" Answering it needs a trace study of the slow runs —
the journals are already on disk — not another A/B of this mechanism.

### Consequences for the mechanism

Deleting it and keeping it are both defensible on this evidence, and neither is
taken here, because choosing now would mean inventing a decision rule after
seeing the data — the exact failure note 0006 was written about:

- **For deletion:** 42 runs with no trigger makes it dead code on the corpus we
  have, and dead code with a trust-boundary argument attached (the note must not
  echo model text) is a maintenance liability.
- **For keeping:** it costs nothing when it does not fire, and the corpus is
  seven tier-3 tasks — absence here is not absence everywhere.

The next gate is pre-registered instead: **if a trace study of the slow runs
finds no literal-repetition episodes either, the nudge and the three-strike stop
are both deleted together**, since a detector that cannot fire is not a safety
feature. If it finds them, the A/B is re-run on cases where the trigger
actually occurs.

## Superseded: the invalid A/B (2026-08-14)

*Everything below is kept as the record of a measurement that did not measure
what it claimed. It is wrong in its conclusion, not in its arithmetic.*

The A/B this note demanded has been run. Seven tier-3 cases × three repetitions
× two arms, n=21 per arm, differing only in the case-JSON field `repeat_nudge`.
Full numbers and failure attribution are in `docs/EVAL_LIVE.md`; the decision
rule was fixed before the run.

| Arm | n | median steps | mean steps | worst run | passed |
|---|---|---|---|---|---|
| control (nudge off) | 21 | 11 | 13.8 | 24 | 19/21 |
| treatment (nudge on) | 21 | 11 | 11.2 | 18 | **21/21** |

Mean paired delta **−2.9 steps** (per-case medians across the three
repetitions; pairing run-by-run instead gives −2.52), and the treatment passed
more cases. Both "keep" conditions were met, so the nudge stays. Four things
keep this from being overstated:

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
- **The two "keep" conditions are therefore not independent evidence.** They
  read like two criteria that happened to agree; they are one case counted
  twice. Dropping `t3-rich-truncate-ellipsis` collapses *both*: passes go to
  18/18 against 18/18, and the delta to −1.33. The pre-registered rule was an
  `OR` over two correlated quantities — a run that dies on
  `step_budget_exhausted` necessarily maxes its step count *and* loses a pass —
  so satisfying both branches was never the confirmation it appears to be. The
  rule was fixed in advance and following it is right; the lesson is about how
  the rule was written, and it is recorded for future gates in note 0006.

**What survives the robustness check is the token figure, not the step
figure.** The control arm spent 3.71M input tokens against the treatment's
2.34M for the same work, and unlike the step effect this does not rest on the
one case or on the two failures:

| Slice | control | treatment | delta |
|---|---|---|---|
| all 21 runs per arm | 3,707,341 | 2,338,465 | −37% |
| dropping `t3-rich-truncate-ellipsis` | 2,849,624 | 2,062,571 | −28% |
| succeeded runs only, per run | 162,978 | 111,355 | −32% |

So the honest basis for keeping the nudge is **cost per unit of work**, which
is stable across every way of slicing the sample, rather than the step delta,
which is not. The step evidence is directionally consistent and imprecise; the
token evidence is the one to cite.
