"""根据工具追踪记录评估 Java 定位运行。

    uv run python -m evals.java.score evals/java/events

评分最终 prose 会变成关键词测试：列出十个候选文件的代理，得分可能和真正找到答案
的代理一样。追踪记录直接回答“定位花了多少工作”，这是索引要减少的量，而且即使
运行没有回答，也能给出数字。

连接两类事件是棘手之处。`tool.proposed` 携带步骤和参数 JSON；`tool.completed`
携带调用 id 和状态，但没有步骤。只有两半都存在且完成状态为 `ok` 时，读取才算命中，
因为被拒绝或未找到的读取从未把文件展示给模型。
"""

from __future__ import annotations

import json
import statistics
import sys
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


def load_events(path: Path) -> list[dict[str, Any]]:
    """从一份 JSONL 追踪中读取事件，并去掉日志封装。"""
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        events.append(record.get("event", record))
    return events


def _argument(event: dict[str, Any], name: str) -> str:
    """提议调用的一个参数。`args_summary` 是截断到 200 个字符的参数 JSON，因此
    长调用可能无法解析；实践中路径通常靠近开头，无法读取的摘要返回空字符串，
    而不是抛出会丢弃整个运行的异常。"""
    raw = event.get("args_summary", "")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return str(parsed.get(name, "")) if isinstance(parsed, dict) else ""


def _succeeded(events: list[dict[str, Any]]) -> set[str]:
    return {
        str(e.get("call_id"))
        for e in events
        if e.get("kind") == "tool.completed" and e.get("status") == "ok"
    }


def _reads(events: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """按顺序返回每个成功完成的 repo.read 的 `(步骤，路径)`。"""
    ok = _succeeded(events)
    out: list[tuple[int, str]] = []
    for event in events:
        if event.get("kind") != "tool.proposed" or event.get("tool_name") != "repo.read":
            continue
        if str(event.get("call_id")) not in ok:
            continue
        path = _argument(event, "path")
        if path:
            out.append((int(event.get("step", 0)), path))
    return out


def _matches(read_path: str, answer: str) -> bool:
    """答案键是相对于仓库的路径；追踪记录可能包含任一种形式。"""
    normalized = read_path.replace("\\", "/").lstrip("./")
    wanted = answer.replace("\\", "/")
    return normalized == wanted or normalized.endswith("/" + wanted)


def steps_to_first_correct_read(
    events: list[dict[str, Any]], answer_files: tuple[str, ...]
) -> int | None:
    """代理首次读取答案文件的步骤；没有读取时返回 None。"""
    for step, path in _reads(events):
        if any(_matches(path, answer) for answer in answer_files):
            return step
    return None


def score_run(
    task_id: str, kind: str, events: list[dict[str, Any]], answer_files: tuple[str, ...]
) -> RunScore:
    hit = steps_to_first_correct_read(events, answer_files)
    searches = sum(
        1
        for e in events
        if e.get("kind") == "tool.proposed" and e.get("tool_name") == "repo.search"
    )
    return RunScore(
        task_id=task_id,
        kind=kind,
        found=hit is not None,
        steps_to_hit=hit,
        total_steps=max((int(e.get("step", 0)) for e in events if "step" in e), default=0),
        files_read=len(_reads(events)),
        searches=searches,
    )


def render(scores: list[RunScore]) -> str:
    """按任务类型分组的 Markdown——unique-name 与 interface-impl 的区分正是本基准
    要产出的发现。"""
    lines = [
        "# Java localization benchmark",
        "",
        "| Kind | n | found | median steps to hit |",
        "|---|---|---|---|",
    ]
    for kind in sorted({s.kind for s in scores}):
        group = [s for s in scores if s.kind == kind]
        hits = [s.steps_to_hit for s in group if s.steps_to_hit is not None]
        median = statistics.median(hits) if hits else float("nan")
        lines.append(f"| {kind} | {len(group)} | {len(hits)}/{len(group)} | {median} |")
    lines += [
        "",
        "| Task | kind | found | steps to hit | total steps | files read | searches |",
        "|---|---|---|---|---|---|---|",
    ]
    for score in sorted(scores, key=lambda s: (s.kind, s.task_id)):
        lines.append(
            f"| {score.task_id} | {score.kind} | {'yes' if score.found else 'NO'} | "
            f"{score.steps_to_hit} | {score.total_steps} | {score.files_read} | {score.searches} |"
        )
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    from evals.java.tasks import TASKS

    events_dir = Path(sys.argv[1])
    scores = []
    missing = []
    for task in TASKS:
        path = events_dir / f"{task.id}.jsonl"
        if not path.is_file():
            missing.append(task.id)
            continue
        scores.append(score_run(task.id, task.kind, load_events(path), task.answer_files))

    report = render(scores)
    if missing:
        report += "\n\nNo trace for: " + ", ".join(missing) + "\n"
    print(report)
    out = Path(__file__).resolve().parent / "report.md"
    out.write_text(report + "\n", encoding="utf-8")
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
