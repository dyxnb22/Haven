"""不收敛的运行实际上是什么样？

ADR 0023 将主要实时失败归因于预算尾部不收敛，却没有检查其具体形态。重复提示
建立在“不收敛看起来像重复”的假设上；用于验证这一点的 A/B 因检测器从未触发而
作废（`docs/notes/rejected/0002`）。本脚本读取该实验留下的运行日志，直接回答
前述问题。

    uv run python evals/trace_study.py evals/real/report-nudge-*/report-live-events

读取 `tool.proposed`（包含步骤、工具名和参数摘要），再通过 call id 与
`tool.completed` 连接。参数和结果字符串是日志保存的截断摘要，因此两个真正不同
但共享长前缀的结果在这里会被视为相同——这种偏差会*高估*重复次数，对于当前问题
来说是更保守的方向。
"""

from __future__ import annotations

import collections
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 达到或超过此步数的运行视为未收敛。A/B 样本的中位数为 11，步数上限为 24。
SLOW_STEPS = 15
#: 不超过此步数的运行视为顺利收敛。
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
        """工具、参数和*结果*都相同的相邻调用数——正是 `StuckLoopDetector` 比较的内容。"""
        keys = [(c.tool, c.args, c.result) for c in self.calls]
        return sum(1 for i in range(1, len(keys)) if keys[i] == keys[i - 1])

    @property
    def repeated_calls(self) -> int:
        """运行中任意位置重复的相同（工具、参数）调用数，无论是否相邻——这是
        *窗口式*检测器可能捕获的内容。"""
        counts = collections.Counter((c.tool, c.args) for c in self.calls)
        return sum(v - 1 for v in counts.values() if v > 1)

    @property
    def tool_mix(self) -> collections.Counter[str]:
        return collections.Counter(c.tool for c in self.calls)


def read_trace(path: Path) -> RunTrace:
    """将一次运行日志读取为有序调用列表。"""
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
    """比较慢运行组和快速运行组。"""
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
