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

## Case coverage (26 cases)

| Category | Count | Examples |
|---|---:|---|
| task | 8 | fix a bug, fix a default, guard empty input, dedup a function, locate a bug, create a regression test, rename with `replace_all`, plan-driven multi-step fix |
| robustness | 4 | invalid arguments recovery, unknown tool, provider error, check timeout |
| security | 7 | parent-dir escape, absolute path, protected `.git` edit, reject-all, create outside the workspace, create cannot overwrite, review blocks a committed AWS key |
| injection | 3 | README → read `~/.ssh`, tool-output injected recipe, edit `.haven.toml` |
| budget | 2 | stuck-loop detection, step-budget exhaustion |
| recovery | 2 | crash where edit never ran (resumable), ambiguous crash (blocked) |

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

## Running

```bash
uv run python evals/generate_cases.py       # regenerate case JSON (after edits)
uv run haven eval --offline                 # full suite → eval_report/
uv run haven eval --offline --category security,injection
uv run pytest tests/eval                    # the suite as a CI gate
```
