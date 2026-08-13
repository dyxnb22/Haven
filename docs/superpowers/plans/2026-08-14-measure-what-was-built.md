# Measure What Was Built: nudge A/B, Java localization, Java oracle tier

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Settle the project's one outstanding "built but unproven" mechanism (the repetition nudge), and extend the measurement apparatus to a real large Java repository so the LSP deferral is decided at a difficulty that matters.

**Architecture:** Three independent workstreams sharing one live-eval harness. A adds a per-case A/B toggle for the nudge and compares *step-count distributions* (continuous, higher statistical power than pass/fail on a variance-driven failure). B generalises an existing, already-accepted sandbox carve-out so a check recipe can declare the toolchain roots it needs, which is what lets Maven run confined. C scores localization from the **tool trace** rather than answer text, which is what an LSP would actually change.

**Tech Stack:** Python 3.12 · uv · pytest · existing `evals/real` harness (`build.py` → case JSON → `haven eval --live`) · DeepSeek beta endpoint · Maven 3 offline (`mvn -o`) against a populated `~/.m2`

## Global Constraints

- Every code change is TDD: failing test first, watched fail, then minimal implementation.
- All gates must pass before any task is considered done: `uv run python scripts/gates.py --mode full` (11 gates).
- No ADR may be edited in place to remove a wrong claim; corrections are annotations (`docs/notes/implemented/0004`), and `scripts/check_adr_links.py` enforces backlinks.
- Live runs cost real money against `DEEPSEEK_API_KEY`. Historical reference: 65 cases ≈ $0.18, ≈50 min wall clock.
- Provider config for every live run:
  `HAVEN_API_KEY_ENV=DEEPSEEK_API_KEY HAVEN_BASE_URL=https://api.deepseek.com/beta HAVEN_MODEL=deepseek-chat`
- The security boundary may only be widened through an ADR plus an offline eval case that proves the *narrow* case still denies (Task B1 before B2).
- `evals/real/{repos,fixtures,cases}` are gitignored; only `tasks.py`, `build.py`, `repos.lock` are tracked.
- A negative result is a deliverable. If Task A4's data shows no improvement, the nudge is **deleted**, per `docs/notes/implemented/0002`.

---

## Scheduling

Wall clock is the binding constraint, not cost. Order is chosen so the longest
API job starts first and the code work happens while it runs.

| Order | Task | Needs API | Est. |
|---|---|---|---|
| 1 | A1 nudge toggle | no | 30 min |
| 2 | A2 A/B analysis script | no | 30 min |
| 3 | **A3 launch both arms (background)** | yes | 60–90 min unattended |
| 4 | B1 ADR 0029 | no | 30 min |
| 5 | B2+B3 readable roots | no | 45 min |
| 6 | B4 security eval case | no | 30 min |
| 7 | A4 read A3's results, verdict | no | 30 min |
| 8 | C1 Java tasks + answer key | no | 45 min |
| 9 | C2 trace scorer | no | 30 min |
| 10 | **C3 run benchmark (background)** | yes | 30–45 min |
| 11 | D1–D3 Java oracle tier | yes | 60 min+ (stretch) |
| 12 | E writeups | no | 30 min |

**Stretch marker:** D is the first thing to drop if time runs out. C is more
informative than D for the LSP question and does not depend on B.

---

## Task A1: A per-case nudge toggle

**Files:**
- Modify: `src/haven/application/run_service.py` (constructor, ~line 176; nudge branch, ~line 771)
- Modify: `src/haven/evalkit/runner.py` (case field ~line 103; service construction ~line 531)
- Modify: `tests/integration/harness.py` (forward the flag)
- Test: `tests/integration/test_agent_journeys.py` (new test in `TestBudgetsAndStops`)

**Interfaces:**
- Consumes: `StuckLoopDetector.observe() -> "ok" | "nudge" | "stuck"` (already exists, `src/haven/domain/stuck.py`)
- Produces: `RunService(..., repeat_nudge: bool = True)`; eval case JSON field `"repeat_nudge": false` disables it. Task A3 relies on both names.

**Precedent to follow:** `context_chars_override` is the existing A/B hook —
a `RunService` constructor parameter fed from a case-JSON field
(`runner.py:103` declares `max_context_chars: int = 0`, `runner.py:531` passes
`context_chars_override=case.max_context_chars`). Copy that shape exactly
rather than inventing an env var; the project's config provenance depends on
knobs being explicit.

- [ ] **Step 1: Write the failing test**

In `tests/integration/test_agent_journeys.py`, inside `class TestBudgetsAndStops`:

```python
async def test_the_nudge_can_be_disabled_for_an_ab_arm(self, tmp_path: Path) -> None:
    """The A/B needs a control arm. Disabling the nudge must remove the
    note from the transcript while leaving the stop behaviour intact."""
    repeat: list[ModelEvent] = [
        tool("c1", "repo.search", pattern="nothing_here", path="."),
        finish("tool_calls"),
    ]
    turns: list[list[ModelEvent]] = [repeat, list(repeat), [text("Done."), finish()]]
    h = Harness(make_repo(tmp_path), turns, repeat_nudge=False)
    outcome = await h.service.run("Search twice")

    assert outcome.status is RunStatus.SUCCEEDED
    assert not any("identical" in m.content for m in h.model.requests_seen[-1].messages), (
        "the control arm must see no harness note"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest "tests/integration/test_agent_journeys.py::TestBudgetsAndStops::test_the_nudge_can_be_disabled_for_an_ab_arm" -q`
Expected: FAIL with `TypeError: Harness.__init__() got an unexpected keyword argument 'repeat_nudge'`

- [ ] **Step 3: Add the constructor parameter**

In `src/haven/application/run_service.py`, add to `RunService.__init__` signature after `supports_prefix_continuation`:

```python
repeat_nudge: bool = (True,)
```

and in the body, after `self._supports_prefix = ...`:

```python
        # A/B control (docs/notes/implemented/0002): the nudge is an unproven
        # convergence intervention, so an eval arm must be able to switch it
        # off without a second build. Default on; only the eval turns it off.
        self._repeat_nudge = repeat_nudge
```

- [ ] **Step 4: Gate the nudge branch on it**

In `run_service.py`, change the nudge branch condition:

```python
            if verdict == "nudge" and self._repeat_nudge:
```

Leave the `elif verdict == "stuck":` branch untouched — the control arm must
still stop on repetition, or the arms differ in two ways at once.

- [ ] **Step 5: Forward it through the harness**

In `tests/integration/harness.py`, add to `Harness.__init__` after
`supports_prefix_continuation`:

```python
repeat_nudge: bool = (True,)
```

and pass it in the `RunService(...)` call:

```python
repeat_nudge = (repeat_nudge,)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest "tests/integration/test_agent_journeys.py::TestBudgetsAndStops" -q`
Expected: PASS, 5 tests (the three existing plus the nudge pair plus the new one)

- [ ] **Step 7: Wire the eval case field**

In `src/haven/evalkit/runner.py`, add to the case dataclass beside
`max_context_chars: int = 0`:

```python
    repeat_nudge: bool = True
```

and at the `RunService(...)` construction beside `context_chars_override=`:

```python
repeat_nudge = (case.repeat_nudge,)
```

- [ ] **Step 8: Verify the full suite and gates**

Run: `uv run python scripts/gates.py --mode fast`
Expected: `7/7 gates passed (fast)`

Run: `uv run pytest -q`
Expected: all pass, count is previous total + 1

- [ ] **Step 9: Commit**

```bash
git add src/haven/application/run_service.py src/haven/evalkit/runner.py tests/integration/harness.py tests/integration/test_agent_journeys.py
git commit -m "feat: per-case repeat-nudge toggle for the convergence A/B"
```

---

## Task A2: The A/B analysis script

**Files:**
- Create: `evals/nudge_ab.py`
- Test: `tests/unit/test_nudge_ab.py`

**Interfaces:**
- Consumes: two live report JSONs written by `haven eval --live --out <dir>`; each has a `cases` list whose entries carry `id`, `steps`, `status`, `stop_reason`.
- Produces: `summarize(cases) -> ArmSummary` and `compare(control, treatment) -> str` (a Markdown report). Task A4 runs this.

**Why step count is the primary metric:** the tier-3 failures are
variance-driven — `t3-click-show-default` took 17 steps in one run and 18 in
another, both over the ceiling, while sibling cases converged in 11–16. With
N=5 per arm, a pass/fail comparison has almost no power. Step count is
continuous, is the quantity the nudge is supposed to move, and yields a signal
from every run including the passing ones.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_nudge_ab.py`:

```python
"""Comparing two A/B arms by step distribution rather than pass/fail.

The failure this experiment targets is variance-driven, so a binary metric at
N=5 would be unable to distinguish a real effect from noise. Steps are
continuous and are exactly what the intervention is supposed to reduce.
"""

from evals.nudge_ab import ArmSummary, compare, summarize


def _case(case_id: str, steps: int, status: str = "succeeded") -> dict:
    return {"id": case_id, "steps": steps, "status": status, "stop_reason": "final_answer"}


class TestSummarize:
    def test_it_reports_median_and_pass_rate(self) -> None:
        arm = summarize([_case("a", 5), _case("b", 9), _case("c", 20, "stopped")])
        assert arm.n == 3
        assert arm.median_steps == 9
        assert arm.passed == 2

    def test_an_empty_arm_is_not_a_crash(self) -> None:
        assert summarize([]).n == 0


class TestCompare:
    def test_it_pairs_cases_by_id(self) -> None:
        control = [_case("a", 20), _case("b", 10)]
        treatment = [_case("a", 12), _case("b", 10)]
        report = compare(control, treatment)
        assert "a" in report and "-8" in report

    def test_it_states_the_verdict_when_there_is_no_improvement(self) -> None:
        same = [_case("a", 10), _case("b", 12)]
        report = compare(same, list(same))
        assert "no improvement" in report.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_nudge_ab.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.nudge_ab'`

- [ ] **Step 3: Write the implementation**

Create `evals/nudge_ab.py`:

```python
"""Compare two live-eval arms of the repetition-nudge experiment.

    uv run python evals/nudge_ab.py CONTROL_REPORT.json TREATMENT_REPORT.json

The nudge (docs/notes/implemented/0002) is an unproven convergence
intervention; ADR 0023 requires a before/after measurement before it stays.
Steps are the primary metric because the failure it targets is a variance
tail, not a hard wall: pass/fail at this sample size cannot separate the two.
"""

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ArmSummary:
    n: int
    median_steps: float
    mean_steps: float
    passed: int


def summarize(cases: list[dict[str, Any]]) -> ArmSummary:
    """Aggregate one arm. An empty arm reports zeros rather than raising."""
    if not cases:
        return ArmSummary(0, 0.0, 0.0, 0)
    steps = [int(c.get("steps", 0)) for c in cases]
    passed = sum(1 for c in cases if c.get("status") == "succeeded")
    return ArmSummary(
        n=len(cases),
        median_steps=statistics.median(steps),
        mean_steps=round(statistics.fmean(steps), 1),
        passed=passed,
    )


def compare(control: list[dict[str, Any]], treatment: list[dict[str, Any]]) -> str:
    """A Markdown comparison, paired by case id."""
    c_sum, t_sum = summarize(control), summarize(treatment)
    by_id_c = {c["id"]: c for c in control}
    by_id_t = {c["id"]: c for c in treatment}
    shared = sorted(set(by_id_c) & set(by_id_t))

    lines = [
        "# Repetition nudge A/B",
        "",
        "| Arm | n | median steps | mean steps | passed |",
        "|---|---|---|---|---|",
        f"| control (nudge off) | {c_sum.n} | {c_sum.median_steps} | {c_sum.mean_steps} | {c_sum.passed} |",
        f"| treatment (nudge on) | {t_sum.n} | {t_sum.median_steps} | {t_sum.mean_steps} | {t_sum.passed} |",
        "",
        "| Case | control steps | treatment steps | delta |",
        "|---|---|---|---|",
    ]
    deltas = []
    for case_id in shared:
        cs, ts = int(by_id_c[case_id]["steps"]), int(by_id_t[case_id]["steps"])
        delta = ts - cs
        deltas.append(delta)
        lines.append(f"| {case_id} | {cs} | {ts} | {delta:+d} |")

    mean_delta = round(statistics.fmean(deltas), 1) if deltas else 0.0
    lines += [
        "",
        f"Mean paired delta: **{mean_delta:+.1f} steps** (negative = nudge converged sooner).",
    ]
    if mean_delta >= 0 and t_sum.passed <= c_sum.passed:
        lines.append("")
        lines.append(
            "**Verdict: no improvement.** Per docs/notes/implemented/0002 the honest "
            "action is to delete the nudge rather than keep it on plausibility."
        )
    return "\n".join(lines)


def _cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("cases", []))


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    report = compare(_cases(Path(sys.argv[1])), _cases(Path(sys.argv[2])))
    print(report)
    Path("eval_report/nudge-ab.md").write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_nudge_ab.py -q`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add evals/nudge_ab.py tests/unit/test_nudge_ab.py
git commit -m "feat: step-distribution comparison for the nudge A/B"
```

---

## Task A3: Run both arms

**Files:**
- Modify: `evals/real/build.py` (emit `repeat_nudge` into case JSON for the control build)
- Output: `evals/real/report-nudge-control/`, `evals/real/report-nudge-treatment/`

**Case set (fixed, chosen from prior reports where these hit the ceiling):**
`t3-click-show-default`, `t3-click-bool-onoff`, `t3-click-echo-stderr`,
`t3-click-nargs-order`, `t3-click-range-clamp`, `t3-jinja-default-filter`,
`t3-rich-truncate-ellipsis`. Seven cases × 2 arms = 14 runs per repetition.

- [ ] **Step 1: Build the fixtures and cases**

Run: `uv run python evals/real/build.py`
Expected: fixtures and case JSON written under `evals/real/{fixtures,cases}`.

- [ ] **Step 2: Produce the control cases**

Write `evals/real/make_control_cases.py`:

```python
"""Copy tier-3 A/B cases with the repetition nudge disabled (control arm)."""

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "cases"
DEST = HERE / "cases-nudge-control"
CASE_IDS = (
    "t3-click-show-default",
    "t3-click-bool-onoff",
    "t3-click-echo-stderr",
    "t3-click-nargs-order",
    "t3-click-range-clamp",
    "t3-jinja-default-filter",
    "t3-rich-truncate-ellipsis",
)

if DEST.exists():
    shutil.rmtree(DEST)
DEST.mkdir(parents=True)
for case_id in CASE_IDS:
    path = SRC / f"{case_id}.json"
    case = json.loads(path.read_text(encoding="utf-8"))
    case["repeat_nudge"] = False
    (DEST / path.name).write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
print(f"wrote {len(CASE_IDS)} control cases to {DEST}")
```

Also create `evals/real/cases-nudge-treatment/` the same way but leaving
`repeat_nudge` at its default (omit the key), so the two directories differ in
exactly one field.

Run: `uv run python evals/real/make_control_cases.py`
Expected: `wrote 7 control cases to .../cases-nudge-control`

- [ ] **Step 3: Launch the treatment arm in the background**

```bash
export DEEPSEEK_API_KEY=... HAVEN_API_KEY_ENV=DEEPSEEK_API_KEY \
       HAVEN_BASE_URL=https://api.deepseek.com/beta HAVEN_MODEL=deepseek-chat
uv run haven eval --live --yes \
  --cases evals/real/cases-nudge-treatment \
  --out evals/real/report-nudge-treatment
```

Expected: a summary line and `report-live.json` in the out directory.

- [ ] **Step 4: Launch the control arm**

```bash
uv run haven eval --live --yes \
  --cases evals/real/cases-nudge-control \
  --out evals/real/report-nudge-control
```

- [ ] **Step 5: Repeat both arms twice more**

Rerun steps 3–4 into `-2` and `-3` suffixed output directories. Three
repetitions of 7 cases per arm gives n=21 per arm, which is the minimum for the
median to mean anything on a variance-driven metric. **Do not** interleave a
code change between repetitions.

---

## Task A4: The verdict

- [ ] **Step 1: Produce the comparison**

Run: `uv run python evals/nudge_ab.py evals/real/report-nudge-control/report-live.json evals/real/report-nudge-treatment/report-live.json`
Expected: a Markdown table on stdout and `eval_report/nudge-ab.md` written.

- [ ] **Step 2: Apply the pre-agreed decision rule**

- Mean paired delta ≤ **−2 steps**, or treatment pass count higher: **keep**. Record the numbers in `docs/EVAL_LIVE.md` and update `docs/notes/implemented/0002` from "unproven" to measured.
- Delta between −2 and 0 with equal passes: **keep but mark inconclusive**, and say so in the note — do not claim a win.
- Delta ≥ 0 and passes not higher: **delete the nudge.** This is the pre-committed outcome.

- [ ] **Step 3: If deleting, revert cleanly**

```bash
# domain: drop the nudge tier, restore a two-state verdict
# run_service: delete the `verdict == "nudge"` branch and `self._repeat_nudge`
# tests: delete the nudge tests, keep the stuck tests
```
Then update `docs/notes/implemented/0002` → move to `docs/notes/rejected/` with
the measured numbers as the reason, and run `uv run python scripts/gates.py --mode full`.

---

## Task B1: ADR 0029 — recipe-declared toolchain roots

**Files:**
- Create: `docs/adr/0029-recipe-declared-toolchain-roots.md`

**The argument this ADR must make** (it is narrower than "widen the sandbox"):
`ports/sandbox.py:57` already carries this exact exception, hardcoded —
`default_readable_roots()` returns the interpreter prefixes with the comment
*"so a virtualenv under $HOME can still be executed by a check recipe"*. Haven
has therefore already accepted that a check recipe may read a toolchain cache
under `$HOME`; it just hardcoded the one toolchain it knew about (Python). A
Maven build needs `~/.m2` for exactly the same reason. The decision is to turn
a hardcoded carve-out into a declared one, with the same trust argument ADR
0013 makes for check recipes: the argv comes from user-authored config, not
from the model.

- [ ] **Step 1: Write the ADR**

Required sections, matching the house format: Status (`Accepted (extends ADR 0009/0013)`),
Context, Decision, "What this does and does not change", Gate: metrics, Gate: risks, Rollback.

Decision content:
- `RecipeSpec` gains `readable_roots: tuple[str, ...] = ()`, parsed from
  `.haven.toml` like `allow_network` already is (`config.py:148`).
- Paths are resolved and expanded at config load, never at run time, so the
  model cannot influence them.
- **`repo.exec` is unaffected.** Only `repo.check` can declare roots, because
  only its argv is user-authored. Model-proposed exec keeps
  `default_readable_roots()` exactly.
- A declared root is **read-only**; it never becomes writable.

Risks to state:
- A user could declare `/` and undo the read confinement for check recipes.
  Accepted as the same class of authority as `allow_network` and the recipe
  argv itself; a recipe already runs arbitrary user-authored code.
- Widening reads for a *check* still cannot exfiltrate through the network
  unless the recipe also sets `allow_network`, but check output does reach the
  transcript — the ADR must say this plainly (this is the ADR 0026 lesson).

- [ ] **Step 2: Verify the backlink gate**

Run: `uv run python scripts/check_adr_links.py`
Expected: `ADR cross-links: 29 ADRs, every overturned one points forward`
(ADR 0029 extends rather than overturns, so no backlink is owed.)

- [ ] **Step 3: Commit**

```bash
git add docs/adr/0029-recipe-declared-toolchain-roots.md
git commit -m "docs: ADR 0029 — recipe-declared toolchain roots"
```

---

## Task B2: Implement declared roots

**Files:**
- Modify: `src/haven/contracts/tools.py:314-323` (`RecipeSpec`)
- Modify: `src/haven/config.py:148` (parsing)
- Modify: `src/haven/adapters/process_executor.py:51-55` (the recipe `SandboxSpec`)
- Test: `tests/unit/test_config.py`, `tests/integration/test_check_sandbox.py`

**Interfaces:**
- Produces: `RecipeSpec.readable_roots: tuple[str, ...]`; `ProcessExecutor.run_recipe` merges
  `default_readable_roots() + tuple(Path(r) for r in spec.readable_roots)` into `SandboxSpec.extra_readable_roots`.
  Task D2 relies on this field name.

- [ ] **Step 1: Write the failing config test**

In `tests/unit/test_config.py`:

```python
def test_a_recipe_may_declare_a_readable_toolchain_root(tmp_path: Path) -> None:
    """A Maven or Gradle check needs its dependency cache under $HOME, the same
    exception the interpreter prefixes already get (ports/sandbox.py)."""
    (tmp_path / ".haven.toml").write_text(
        '[recipes.mvn]\nargv = ["mvn", "-o", "test"]\nreadable_roots = ["~/.m2"]\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.recipes["mvn"].readable_roots == ("~/.m2",)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_config.py -k readable_toolchain -q`
Expected: FAIL — `AttributeError: 'RecipeSpec' object has no attribute 'readable_roots'`

- [ ] **Step 3: Add the field and parsing**

`contracts/tools.py`, in `RecipeSpec` after `allow_network`:

```python
    #: Extra roots this recipe may READ, on top of the interpreter prefixes
    #: every recipe already gets. A toolchain keeps its dependency cache under
    #: $HOME (`~/.m2`, `~/.gradle`), which the sandbox hides by default. Only a
    #: check recipe may declare these, because only its argv is user-authored
    #: (ADR 0029); never writable, and never available to `repo.exec`.
    readable_roots: tuple[str, ...] = ()
```

`config.py`, in the recipe parsing beside `allow_network`:

```python
readable_roots = (tuple(str(r) for r in spec.get("readable_roots", ())),)
```

- [ ] **Step 4: Thread it into the recipe sandbox**

`adapters/process_executor.py`, in the `SandboxSpec(...)` at line ~51:

```python
extra_readable_roots = (
    (
        *default_readable_roots(),
        *(Path(r).expanduser().resolve() for r in recipe.readable_roots),
    ),
)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_config.py tests/integration/test_check_sandbox.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/haven/contracts/tools.py src/haven/config.py src/haven/adapters/process_executor.py tests/unit/test_config.py
git commit -m "feat: recipes may declare read-only toolchain roots (ADR 0029)"
```

---

## Task B3: Prove the narrow case still denies

**Files:**
- Create: `evals/cases/sec-recipe-root-not-granted.json` (via `evals/generate_cases.py`)
- Modify: `evals/generate_cases.py`
- Test: `tests/security/test_sandbox_enforcement.py`

The point of this task: a widened boundary is only acceptable with a test that
the *un*widened case still fails closed.

- [ ] **Step 1: Write both halves of the enforcement test**

In `tests/security/test_sandbox_enforcement.py`. This uses the file's existing
`launcher` fixture and `SandboxSpec`; it declares one directory and probes a
sibling, so the two assertions differ in exactly one variable.

```python
def test_a_declared_root_is_readable_and_its_sibling_is_not(tmp_path: Path) -> None:
    """The widened boundary must be exactly as wide as declared: the granted
    directory opens, the one next to it stays shut (ADR 0029)."""
    launcher = select_launcher()
    if launcher is None or not launcher.available():
        pytest.skip("no sandbox backend on this platform")

    home = Path.home()
    granted = home / ".haven-probe-granted"
    withheld = home / ".haven-probe-withheld"
    granted.mkdir(exist_ok=True)
    (granted / "f.txt").write_text("GRANTED-MARKER", encoding="utf-8")
    withheld.mkdir(exist_ok=True)
    (withheld / "f.txt").write_text("WITHHELD-MARKER", encoding="utf-8")
    try:
        spec = SandboxSpec(
            workspace_root=tmp_path,
            scratch_dir=tmp_path / "scratch",
            writable=False,
            allow_network=False,
            private_roots=default_private_roots(),
            extra_readable_roots=(*default_readable_roots(), granted),
        )
        (tmp_path / "scratch").mkdir(exist_ok=True)
        ok = _run_confined(launcher, spec, ["cat", str(granted / "f.txt")])
        nope = _run_confined(launcher, spec, ["cat", str(withheld / "f.txt")])
        assert "GRANTED-MARKER" in ok.stdout
        assert "WITHHELD-MARKER" not in nope.stdout
    finally:
        shutil.rmtree(granted, ignore_errors=True)
        shutil.rmtree(withheld, ignore_errors=True)
```

Add the helper next to it (the file already spawns confined commands this way;
match whatever the neighbouring tests use rather than introducing a second style):

```python
def _run_confined(launcher, spec: SandboxSpec, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run argv under the sandbox and capture its output."""
    wrapped = launcher.wrap(argv, spec)
    return subprocess.run(wrapped, capture_output=True, text=True, timeout=30)
```

- [ ] **Step 2: Run it and confirm both halves**

Run: `uv run pytest tests/security/test_sandbox_enforcement.py -k declared_root -q`
Expected: PASS on macOS. If the *withheld* assertion fails, B2 granted more
than it declared — fix B2, never relax the assertion.

- [ ] **Step 4: Full gates**

Run: `uv run python scripts/gates.py --mode full`
Expected: `11/11 gates passed (full)`

- [ ] **Step 5: Commit**

```bash
git add tests/security/test_sandbox_enforcement.py evals/generate_cases.py evals/cases
git commit -m "test: declared roots do not open the rest of \$HOME"
```

---

## Task C1: Java localization tasks and answer key

**Files:**
- Create: `evals/java/tasks.py`
- Create: `evals/java/README.md`

**Repo:** `/Users/diaoyuxuan/big-market-ai-platform` at commit `d6d675d`
(534 Java files, ~32k lines, 12+ Maven modules). Pin the SHA in the README; do
not modify the repo — every task in this workstream is read-only.

**Why this repo answers the LSP question better than tier 3:** Java with
dependency injection, interfaces, and method overloading is the profile where
`repo.search` should lose to an index. Tier 3 is single-language Python
libraries with grep-friendly unique names — the regime where grep is most
competitive (see the amendment in ADR 0023).

- [ ] **Step 1: Author 10 localization tasks**

Each task is a question whose answer is one specific file, chosen to span the
difficulty range. Write them as:

```python
@dataclass(frozen=True)
class LocalizationTask:
    id: str
    goal: str  # what the agent is asked
    answer_files: tuple[str, ...]  # repo-relative; reading any one counts as found
    kind: str  # "unique-name" | "overloaded" | "interface-impl" | "di-wiring"
```

Cover, at minimum: 3 `unique-name` (grep should win), 3 `interface-impl`
(find the implementation behind an interface — grep returns the interface),
2 `overloaded` (a common method name with many hits), 2 `di-wiring` (which
bean/config supplies a dependency).

- [ ] **Step 2: Verify every answer key by hand**

For each task, confirm the answer file actually contains the answer, and record
in the README how many `rg` hits the naive query returns — that number is the
baseline difficulty and belongs in the report.

- [ ] **Step 3: Commit**

```bash
git add evals/java/
git commit -m "test: Java localization benchmark tasks and answer key"
```

---

## Task C2: Score localization from the trace

**Files:**
- Create: `evals/java/score.py`
- Test: `tests/unit/test_java_localization_score.py`

**Interfaces:**
- Consumes: a run's event journal (`haven export RUN_ID --format jsonl`), specifically `tool.completed` events for `repo.read`.
- Produces: `steps_to_first_correct_read(events, answer_files) -> int | None` and `score_run(...) -> RunScore`.

**Why score the trace, not the answer text:** grading on whether the final
prose names the file is a keyword probe — an agent that lists ten candidate
files scores as well as one that knew. The trace asks the question directly:
*how much work did localization take?* That is precisely the quantity an LSP
would reduce, and it yields a number even on runs that never answer.

- [ ] **Step 1: Write the failing test**

```python
def test_steps_to_first_correct_read_counts_reads_before_the_hit() -> None:
    events = [
        _read_event(step=1, path="src/main/java/Wrong.java"),
        _read_event(step=2, path="src/main/java/Also.java"),
        _read_event(step=3, path="src/main/java/Right.java"),
    ]
    assert steps_to_first_correct_read(events, ("src/main/java/Right.java",)) == 3


def test_a_run_that_never_reads_the_answer_scores_none() -> None:
    events = [_read_event(step=1, path="src/main/java/Wrong.java")]
    assert steps_to_first_correct_read(events, ("src/main/java/Right.java",)) is None
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/unit/test_java_localization_score.py -q`
Expected: FAIL — module missing

- [ ] **Step 3: Write the implementation**

Create `evals/java/score.py`:

```python
"""Score Java localization runs from the tool trace.

Grading the final prose would be a keyword probe: an agent that lists ten
candidate files scores like one that knew the answer. The trace answers the
question directly — how much work did localization take — which is the quantity
an index would reduce.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RunScore:
    task_id: str
    kind: str
    found: bool
    steps_to_hit: int | None
    total_steps: int
    files_read: int
    searches: int


def _read_paths(events: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """(step, path) for every completed repo.read, in order."""
    out = []
    for event in events:
        if event.get("kind") != "tool.completed" or event.get("tool_name") != "repo.read":
            continue
        path = str(event.get("path") or json.loads(event.get("args_json", "{}")).get("path", ""))
        if path:
            out.append((int(event.get("step", 0)), path))
    return out


def steps_to_first_correct_read(
    events: list[dict[str, Any]], answer_files: tuple[str, ...]
) -> int | None:
    """The step at which the agent first read an answer file, or None."""
    wanted = {a.replace("\\", "/") for a in answer_files}
    for step, path in _read_paths(events):
        if path.replace("\\", "/") in wanted:
            return step
    return None


def score_run(
    task_id: str, kind: str, events: list[dict[str, Any]], answer_files: tuple[str, ...]
) -> RunScore:
    hit = steps_to_first_correct_read(events, answer_files)
    return RunScore(
        task_id=task_id,
        kind=kind,
        found=hit is not None,
        steps_to_hit=hit,
        total_steps=max((int(e.get("step", 0)) for e in events), default=0),
        files_read=len(_read_paths(events)),
        searches=sum(
            1
            for e in events
            if e.get("kind") == "tool.completed" and e.get("tool_name") == "repo.search"
        ),
    )


def render(scores: list[RunScore]) -> str:
    """Markdown, grouped by task kind — the unique-name vs interface-impl
    split is the finding this benchmark exists to produce."""
    lines = [
        "# Java localization benchmark",
        "",
        "| Kind | n | found | median steps to hit |",
        "|---|---|---|---|",
    ]
    kinds = sorted({s.kind for s in scores})
    for kind in kinds:
        group = [s for s in scores if s.kind == kind]
        hits = [s.steps_to_hit for s in group if s.steps_to_hit is not None]
        median = statistics.median(hits) if hits else float("nan")
        lines.append(f"| {kind} | {len(group)} | {len(hits)}/{len(group)} | {median} |")
    lines += [
        "",
        "| Task | kind | found | steps to hit | total steps | files read | searches |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in sorted(scores, key=lambda s: (s.kind, s.task_id)):
        lines.append(
            f"| {s.task_id} | {s.kind} | {'yes' if s.found else 'NO'} | {s.steps_to_hit} | "
            f"{s.total_steps} | {s.files_read} | {s.searches} |"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests and commit**

Run: `uv run pytest tests/unit/test_java_localization_score.py -q`
Expected: PASS, 2 tests

```bash
git add evals/java/score.py tests/unit/test_java_localization_score.py
git commit -m "test: score Java localization from the tool trace"
```

---

## Task C3: Run the benchmark

- [ ] **Step 1: Run each task read-only against the real model**

```bash
uv run haven run "<task goal>" --workspace /Users/diaoyuxuan/big-market-ai-platform \
  --jsonl --events evals/java/events/<task-id>.jsonl
```

Read-only is the default for `haven run`, so no `--write` and no approval
policy is needed, and the repo cannot be modified.

- [ ] **Step 2: Score and report**

Run: `uv run python evals/java/score.py evals/java/events`
Expected: a Markdown table written to `evals/java/report.md`, broken down by task kind.

- [ ] **Step 3: Interpret against the LSP gate**

The pre-registered reading:
- `interface-impl` and `overloaded` medians **much worse** than `unique-name`,
  with several `found = false`: this is the semantic-localization evidence ADR
  0023's gate asked for. Count the failures and compare against the ≥5 bar.
- All kinds comparable: grep localization holds up even in Java, and the LSP
  deferral is confirmed at a difficulty that matters — a stronger result than
  the current one.
- Runs dying on budget before localization is even tested: **the ceiling binds
  first**, which is its own finding and the thing to fix before any index.

---

## Task D (stretch): Java write tier with the Maven oracle

Depends on Task B. Only start this if A and C are complete.

- [ ] **D1:** Add the repo to a new `evals/java/repos.lock` pinned at `d6d675d`; build a case with
  `recipes: {"verify": {"argv": ["mvn", "-o", "-pl", "<module>", "test"], "readable_roots": ["~/.m2"], "timeout_seconds": 300}}`.
- [ ] **D2:** Confirm the recipe runs confined:
  `uv run haven run "run the verify recipe" --workspace <repo> --write --approval-policy trusted-recipe`
  Expected: `Tests run: 4, Failures: 0` inside the sandbox. If `~/.m2` is still unreachable, B2 is wrong — fix B2, do not weaken the test.
- [ ] **D3:** Inject one bug into a module covered by a pure unit test (`big-market-types` has 4 passing tests), and run the full task: the agent must find and fix it, with the Maven run as the Evidence Gate's check.

---

## Task E: Write up

- [ ] **Step 1:** Add a section to `docs/EVAL_LIVE.md` for each experiment run, in the existing format: what was measured, the numbers, and the attribution of every non-passing run.
- [ ] **Step 2:** Update `docs/notes/implemented/0002` with the nudge verdict (or move it to `rejected/`).
- [ ] **Step 3:** Amend ADR 0023's LSP section with the Java results — this is the amendment the ADR itself pre-registered.
- [ ] **Step 4:** `uv run python scripts/refresh_metrics.py` then `uv run python scripts/gates.py --mode full`.
- [ ] **Step 5:** Commit.

---

## Kill criteria and fallbacks

- **A3 costs more than $5 or 2 hours:** stop after two repetitions; report n=14 per arm and say the power is lower than planned.
- **Maven cannot run confined after B2:** stop workstream D, keep B (the ADR and the mechanism stand on the Python virtualenv precedent), and record the failure in the ADR's risks.
- **The Java repo turns out to need a database for the tasks chosen:** C is unaffected (read-only), D drops to the `big-market-types` module only.
- **The nudge A/B is inconclusive:** say inconclusive. Do not upgrade a null result into a win, and do not delete on a null result either — the note already commits to "no improvement → delete", and inconclusive is a third outcome that needs more n, not a decision.
