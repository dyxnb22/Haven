"""What does a non-converging run actually look like?

ADR 0023 attributed the dominant live failure to budget-tail non-convergence and
left the shape of it unexamined. The repetition nudge was built on the assumption
that non-convergence looks like repetition; the A/B meant to test it was void
because the detector never fired (docs/notes/rejected/0002). This reads the run
journals that experiment left behind and asks the prior question directly.

    uv run python evals/trace_study.py evals/real/report-nudge-*/report-live-events

Reads `tool.proposed` (which carries step, tool name, and an argument summary)
joined to `tool.completed` by call id. The argument and result strings are the
clipped summaries the journal stores, so two genuinely different results sharing
a long prefix would read as identical here — the bias is toward *over*-counting
repetition, which is the conservative direction for the question being asked.
"""

from __future__ import annotations

import collections
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: A run at or above this many steps is treated as non-converging. The A/B
#: corpus medians at 11, and the step ceiling is 24.
SLOW_STEPS = 15
#: A run at or below this converged without difficulty.
FAST_STEPS = 11


@dataclass(frozen=True, slots=True)
class Call:
    step: int
    tool: str
    args: str
    result: str


@dataclass(slots=True)
class RunTrace:
    name: str
    calls: list[Call] = field(default_factory=list)

    @property
    def steps(self) -> int:
        return self.calls[-1].step if self.calls else 0

    @property
    def consecutive_identical(self) -> int:
        """Adjacent calls identical in tool, args, *and* result — exactly what
        `StuckLoopDetector` compares."""
        keys = [(c.tool, c.args, c.result) for c in self.calls]
        return sum(1 for i in range(1, len(keys)) if keys[i] == keys[i - 1])

    @property
    def repeated_calls(self) -> int:
        """Repeats of the same (tool, args) anywhere in the run, adjacent or
        not — what a *windowed* detector could catch."""
        counts = collections.Counter((c.tool, c.args) for c in self.calls)
        return sum(v - 1 for v in counts.values() if v > 1)

    @property
    def tool_mix(self) -> collections.Counter[str]:
        return collections.Counter(c.tool for c in self.calls)


def read_trace(path: Path) -> RunTrace:
    """One run journal into an ordered call list."""
    proposed: dict[str, tuple[int, str, str]] = {}
    trace = RunTrace(name=path.stem)
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line).get("event", {})
        except json.JSONDecodeError:
            continue
        kind = event.get("kind")
        if kind == "tool.proposed":
            proposed[event["call_id"]] = (
                int(event.get("step", 0)),
                event.get("tool_name", ""),
                event.get("args_summary", ""),
            )
        elif kind == "tool.completed":
            found = proposed.get(event.get("call_id", ""))
            if found is not None:
                trace.calls.append(Call(found[0], found[1], found[2], event.get("summary", "")))
    return trace


def render(traces: list[RunTrace]) -> str:
    """Compare the slow cohort against the fast one."""
    slow = [t for t in traces if t.steps >= SLOW_STEPS]
    fast = [t for t in traces if t.steps <= FAST_STEPS]
    lines = [
        "# Trace study: the shape of a non-converging run",
        "",
        f"{len(traces)} run journals · slow (>={SLOW_STEPS} steps) {len(slow)} · "
        f"fast (<={FAST_STEPS}) {len(fast)}",
        "",
        "| Cohort | runs | median calls | consecutive identical | runs with any repeat |",
        "|---|---|---|---|---|",
    ]
    for label, group in (("slow", slow), ("fast", fast)):
        if not group:
            continue
        with_repeat = sum(1 for t in group if t.repeated_calls)
        lines.append(
            f"| {label} | {len(group)} | "
            f"{statistics.median([len(t.calls) for t in group]):.0f} | "
            f"{sum(t.consecutive_identical for t in group)} | "
            f"{with_repeat}/{len(group)} |"
        )
    lines += ["", "| Tool | slow per run | fast per run | ratio |", "|---|---|---|---|"]
    slow_mix: collections.Counter[str] = collections.Counter()
    fast_mix: collections.Counter[str] = collections.Counter()
    for t in slow:
        slow_mix.update(t.tool_mix)
    for t in fast:
        fast_mix.update(t.tool_mix)
    for tool, _ in slow_mix.most_common():
        per_slow = slow_mix[tool] / max(1, len(slow))
        per_fast = fast_mix[tool] / max(1, len(fast))
        ratio = f"{per_slow / per_fast:.1f}x" if per_fast else "—"
        lines.append(f"| `{tool}` | {per_slow:.1f} | {per_fast:.1f} | {ratio} |")
    return "\n".join(lines)


def main() -> int:
    roots = [Path(a) for a in sys.argv[1:]]
    if not roots:
        print(__doc__)
        return 2
    traces = [read_trace(f) for root in roots for f in sorted(root.glob("*.jsonl"))]
    traces = [t for t in traces if t.calls]
    if not traces:
        print("no run journals found")
        return 1
    report = render(traces)
    print(report)
    Path("eval_report/trace-study.md").write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
