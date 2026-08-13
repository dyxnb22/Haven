"""Compare two live-eval arms of the repetition-nudge experiment.

    uv run python evals/nudge_ab.py --control REPORT.json [...] --treatment REPORT.json [...]

The nudge (docs/notes/implemented/0002) is an unproven convergence
intervention; ADR 0023 requires a before/after measurement before it stays.
Steps are the primary metric because the failure it targets is a variance
tail, not a hard wall: pass/fail at this sample size cannot separate the two.

Each arm takes several reports because the experiment is repeated. Repetitions
of one case are pooled, not overwritten: the arm summary counts every run, and
the paired table compares the per-case *median* across repetitions, so a single
lucky run cannot carry a case.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
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


def _steps_by_id(cases: list[dict[str, Any]]) -> dict[str, list[int]]:
    """Every run of every case id, keyed by id. Repetitions accumulate."""
    grouped: dict[str, list[int]] = defaultdict(list)
    for case in cases:
        grouped[str(case["id"])].append(int(case.get("steps", 0)))
    return grouped


def compare(control: list[dict[str, Any]], treatment: list[dict[str, Any]]) -> str:
    """A Markdown comparison, paired by case id across repetitions."""
    c_sum, t_sum = summarize(control), summarize(treatment)
    by_id_c, by_id_t = _steps_by_id(control), _steps_by_id(treatment)
    shared = sorted(set(by_id_c) & set(by_id_t))

    lines = [
        "# Repetition nudge A/B",
        "",
        "| Arm | n | median steps | mean steps | passed |",
        "|---|---|---|---|---|",
        f"| control (nudge off) | {c_sum.n} | {c_sum.median_steps} | "
        f"{c_sum.mean_steps} | {c_sum.passed} |",
        f"| treatment (nudge on) | {t_sum.n} | {t_sum.median_steps} | "
        f"{t_sum.mean_steps} | {t_sum.passed} |",
        "",
        "| Case | runs | control steps | treatment steps | delta |",
        "|---|---|---|---|---|",
    ]
    deltas = []
    for case_id in shared:
        control_runs, treatment_runs = by_id_c[case_id], by_id_t[case_id]
        cs = statistics.median(control_runs)
        ts = statistics.median(treatment_runs)
        delta = ts - cs
        deltas.append(delta)
        runs = f"{len(control_runs)}v{len(treatment_runs)}"
        observed = f"{cs:g} ({','.join(str(s) for s in control_runs)})"
        observed_t = f"{ts:g} ({','.join(str(s) for s in treatment_runs)})"
        lines.append(f"| {case_id} | {runs} | {observed} | {observed_t} | {delta:+g} |")

    mean_delta = round(statistics.fmean(deltas), 1) if deltas else 0.0
    lines += [
        "",
        f"Mean paired delta: **{mean_delta:+.1f} steps** "
        "(negative = nudge converged sooner). Per-case values are medians "
        "across repetitions.",
    ]
    if mean_delta >= 0 and t_sum.passed <= c_sum.passed:
        lines.append("")
        lines.append(
            "**Verdict: no improvement.** Per docs/notes/implemented/0002 the honest "
            "action is to delete the nudge rather than keep it on plausibility."
        )
    return "\n".join(lines)


def _cases(paths: list[Path]) -> list[dict[str, Any]]:
    pooled: list[dict[str, Any]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        pooled.extend(data.get("cases", []))
    return pooled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", nargs="+", type=Path, required=True)
    parser.add_argument("--treatment", nargs="+", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("eval_report/nudge-ab.md"))
    args = parser.parse_args()

    report = compare(_cases(args.control), _cases(args.treatment))
    print(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
