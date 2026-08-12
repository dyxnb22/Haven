# Haven Evaluation

Haven ships a fixed, fully offline eval suite so behavior can be measured and
compared across versions without a network or an API key. The suite is the
executable form of the project's claims — especially the security ones.

## How it works

Each case is a reviewable JSON file in `evals/cases/` describing:

- a `fixture` repository copied into a fresh temp dir per run,
- a scripted sequence of model `turns` (the exact `ModelEvent`s the fake model
  emits), so the run is deterministic,
- registered `recipes` (verification commands, `{python}` expands to the test
  interpreter),
- an `expect` block (status, stop reason, gate reason, file assertions, expected
  tool error codes / policy denies, forbidden transcript strings, step caps).

Cases run through the **real** application stack — `RunService`, the tool
pipeline, the filesystem workspace, and the subprocess executor — with only the
model faked. Two invariants are checked on *every* case regardless of its
expectations:

1. no file outside `expect.allowed_changed_files` may change (bytecode ignored);
2. no `transcript_must_not_contain` string may appear in the model transcript.

`evals/generate_cases.py` regenerates the JSON from a single readable source.
`haven eval --offline` runs the suite and writes `eval_report/report.json` and
`report.md`.

## Case coverage

The authoritative case counts live in the generated metrics table (README /
`PROJECT_CARD.md`) and in `eval_report/report.json`; they are not repeated here,
so this list cannot drift. What each category covers:

| Category | What it exercises |
|---|---|
| task | realistic workflows: fix a bug, guard empty input, dedup a function, rename with `replace_all`, a plan-driven multi-step fix, and a composed edit + delete + diff + check run |
| robustness | recovery from bad input: invalid arguments, unknown tool, provider error, check timeout, an unwinnable evidence gate, and a run compacted mid-flight |
| security | boundary holds: path escapes (parent, absolute), protected `.git`, reject-all, create/overwrite rules, a committed-secret review, sandboxed `repo.exec` escapes, an exec write stopped by the read-only workspace profile, and protected-path delete / out-of-workspace move |
| injection | untrusted text cannot change behavior: README → read `~/.ssh`, tool-output injected recipe, edit `.haven.toml` |
| budget | hard limits stop a run: stuck-loop detection, step-budget exhaustion |
| recovery | interrupted effects: an edit that never ran (resumable), an ambiguous crash (blocked) |

## Metrics (reported separately, never averaged into one score)

The JSON report breaks results down by category and records, per case: pass/fail
with failure reasons, terminal status and stop reason, steps, tool calls, input/
output tokens, estimated cost, wall-clock ms, and **unauthorized file changes**.
Security is a hard gate: the suite's `security_violations` count must be `0`, and
the summary line surfaces it explicitly. Quality metrics (task success) and
safety metrics (violations, denies) are kept distinct so a good average can never
paper over a security regression.

## Reproducibility and baselines

Because the model is scripted and the clock/subprocess work is bounded, re-running
the same commit yields the same results. To compare a change (prompt, schema,
context policy), keep the previous `report.json` as a baseline and diff it against
the candidate run; record the trade-off in an ADR or the weekly retro.

## Live eval

Live evaluation against a real provider is intentionally *not* part of CI. It is a
separate, explicitly confirmed, paid command:

```bash
export HAVEN_API_KEY=sk-...
uv run haven eval --live --yes                    # task cases by default
uv run haven eval --live --yes --category task,robustness
```

What changes in live mode:

- the scripted turns are ignored and the real provider drives the run;
- each case still runs in a **disposable copy of its fixture**, never your own
  repository, with approvals auto-granted inside that sandbox;
- only the **outcome** (terminal status + file assertions) and the **security
  invariants** are asserted. Trajectory expectations (exact stop reason, specific
  tool error codes, step caps) are scripted-only and are skipped, because they
  would measure the script rather than the agent;
- the report is written to `report-live.{json,md}`, tagged `"mode": "live"`, and
  carries a banner stating the numbers are not reproducible, plus the estimated
  spend.

The "nothing forbidden reached the model" check runs in **both** modes: every
request actually sent is recorded by a wrapper around the provider, so a live run
still proves that out-of-workspace file contents never entered the transcript.

Only numbers that come from a versioned report belong in a résumé, and live
numbers must be labelled as such.

## Quality and safety are reported apart

The report never averages "did the agent get work done" with "did a guarantee
hold". `SuiteReport` splits categories into **quality** (`task`, `robustness`,
`budget`) and **safety** (`security`, `injection`, `recovery`) and headlines a
task-shaped pass rate next to `security violations: 0`. A task that fails is
quality variance; a security violation is a broken promise, and one must never
be allowed to hide behind the other.

## Real-task evaluation (the honest limit)

The offline suite proves the *stack* supports realistic task shapes — the
`task-refactor-and-cleanup` case, for instance, composes edit + delete + diff +
check into one evidence-satisfied run. What it cannot prove is *model quality*:
the scripted trajectory is fixed, so an offline pass measures the harness, not
the agent's judgement.

Measuring whether Haven actually completes real work needs a **live** suite of
50–100 small-to-medium tasks drawn from real repositories, scored on patch
correctness, test-suite regressions, out-of-scope changes, tokens, wall clock,
and human-approval count. That run costs money and needs a key, so it is
deliberately not bundled here; the harness (`haven eval --live`) and the
quality/safety split above are the scaffolding for it. Until it is run, this
project claims "boundaries hold on 36 scripted cases", not "N% real-task
success" — the distinction is the whole point.

## Running

```bash
uv run python evals/generate_cases.py       # regenerate case JSON (after edits)
uv run haven eval --offline                 # full suite → eval_report/
uv run haven eval --offline --category security,injection
uv run pytest tests/eval                    # the suite as a CI gate
```
