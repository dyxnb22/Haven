# Module 09 — Evaluation and cost

> Files: `src/haven/evalkit/runner.py`, `evals/generate_cases.py`,
> `evals/compare_prompt.py`, `docs/EVAL.md`, `docs/EVAL_LIVE.md`
> Tests: `tests/eval/test_eval_suite.py`
> ADR: [0005 — offline eval and scripted model](../docs/adr/0005-offline-eval-and-scripted-model.md),
> [0008 — prompt-cache prefix stability](../docs/adr/0008-prompt-cache-prefix-stability.md)

## Learning objectives

- Build a reproducible offline eval that doubles as a **security gate**.
- Report metrics separately so a good average cannot hide a security regression.
- Know precisely what only a **live** run can tell you.
- Do cost engineering with a measured before/after and honest caveats.

## Offline eval as a CI gate

Read `evalkit/runner.py`. Each case (JSON in `evals/cases/`, generated from the
readable `evals/generate_cases.py`) runs the **real** stack — loop, pipeline,
filesystem, subprocess — in a disposable copy of a fixture, with only the model
faked. That is what makes it deterministic, free, and safe to run in CI.

Two invariants are checked on *every* case regardless of its own expectations:

- no **protected path** may change (a program-guaranteed boundary);
- no forbidden string may reach the model transcript (secret non-leakage).

These are reported *separately* from "out-of-scope changes" (an in-workspace file
the task did not call for) and from task success. That separation is the point of
ADR 0005: if you average a safety metric into a quality score, a strong quality
number can hide a security regression. `haven eval --offline` prints
`security violations: N` on its own line, and CI fails if it is not 0.

`tests/eval/test_eval_suite.py` runs the whole suite as a normal test, so the
gate cannot rot.

## What only a live run tells you

ADR 0005 keeps live eval out of CI (paid, non-deterministic), but the project
did run it, and `docs/EVAL_LIVE.md` is the report. Read it: **six real defects
were invisible to the mocked contract tests** and surfaced only against a real
model — dropped reasoning content, rejected namespaced tool names, discarded
error bodies, a tool error that aborted the whole run, an unwinnable evidence
gate, and a scope-creep misconfiguration.

The transferable lesson: a fake model validates your *machinery* (does the gate
fire, does recovery classify correctly). It cannot validate your assumptions
about a real provider's wire format or a real model's behavior. You need both,
and you should be able to say which defects each kind of testing can and cannot
catch.

## Cost engineering, measured honestly

The first live run spent ~26k input tokens per short task, which prompted the
prompt-cache work (Module 05, ADR 0008). The method is the lesson:

1. **Measure a baseline.** 71% cache hit on the 8-task suite.
2. **Find the mechanism.** The live budget counter sat in message two; prefix
   caching matches from the front, so everything after it was re-billed.
3. **Fix and re-measure.** Move volatile content to the tail → 89% hit, input
   tokens 127k → 114k.
4. **State what it does not prove.** The "before" was already 71% because the
   fixed system+tools prefix caches either way; the win is the transcript, so it
   *grows with run length* and is only ~10% on these short tasks. The before/after
   pass-count difference (8 vs 7) is model noise, not the change.

`evals/compare_prompt.py` computes the deterministic part (bytes/tokens added by
a context change) offline. The live percentages are in `EVAL_LIVE.md`, labelled
non-reproducible. That combination — deterministic where you can be, clearly
labelled estimates where you cannot — is the standard to hold your own numbers
to.

## Exercises

1. **Add a case.** Write a new offline eval case (edit `generate_cases.py`,
   regenerate) for a task type not yet covered, with explicit
   `allowed_changed_files`. Run `uv run haven eval --offline` and confirm 0
   security violations.
2. **Trip the gate.** Author a case where the scripted model tries to write a
   protected path; confirm it is reported as a security violation and the run is
   blocked by policy.
3. **Category slice.** Run `uv run haven eval --offline --category security,injection`
   and read the report; confirm the count matches the cases you expect.
4. **(Live, optional)** With a key set, run `uv run haven eval --live --yes
   --category task` and read the cache-hit rate in the summary line. Compare to
   `EVAL_LIVE.md`.

## Self-check

- Why must safety metrics be reported separately from task success?
- Name two defect classes only a live run can find, and two that offline eval
  covers better (deterministically).
- In the cache work, what does the 89% number *not* prove on its own?

## Further reading

- ADR 0005 (offline gate) and ADR 0008 (caching).
- `docs/EVAL.md` and `docs/EVAL_LIVE.md` in full.
- Commit `5d77d59` (`feat(eval)`), `9d8e684` / `c754f0f` (caching + accounting).
