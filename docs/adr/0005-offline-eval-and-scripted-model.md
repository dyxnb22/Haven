# ADR 0005: Offline Eval with a Scripted Model

## Status

Accepted

## Context

Testing an agent against a live LLM is slow, costs money, and is
non-deterministic — the same commit can pass or fail run to run, and CI cannot
depend on a paid, flaky external service. But the behaviors that matter most
(policy denials, approval binding, the evidence gate, stuck-loop and budget
stops, crash recovery) are program behaviors that do not need a real model to
exercise.

## Decision

- A `ScriptedModel` implements `ModelPort` by replaying authored `ModelEvent`
  turns from plain JSON. It is the **default** model in tests and eval; the real
  provider is only used behind an explicit, opt-in command.
- The offline eval suite (`evals/`) runs each case through the real application
  stack (loop, pipeline, filesystem, subprocess) with only the model faked, in a
  fresh temp copy of a fixture repo.
- Every case enforces two invariants regardless of expectations: no file outside
  an allow-list may change, and no forbidden string may reach the transcript.
- Metrics are reported per category and never averaged into a single score;
  `security_violations` is a hard gate wired into CI.

## Consequences

- CI is fast, free, deterministic, and safe by default.
- Security and recovery guarantees have executable, regression-guarded evidence.
- Cases must be authored and kept in sync with tool/schema changes
  (`evals/generate_cases.py` centralizes this).
- Scripted turns test the *machinery*, not the model's reasoning quality; live
  eval (separate, labelled, opt-in) is where model quality is measured.

## Alternatives considered

- **Live-model integration tests in CI**: rejected — flaky, paid, non-reproducible.
- **Record/replay real transcripts (VCR-style)**: heavier fixtures and brittle to
  provider changes; scripted turns are smaller and reviewable by hand.
- **One aggregate score**: rejected — lets a strong task score hide a security
  regression; safety metrics are kept separate on purpose.
