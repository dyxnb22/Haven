"""Score Java localization runs from the tool trace.

    uv run python -m evals.java.score evals/java/events

Grading the final prose would be a keyword probe: an agent that lists ten
candidate files scores like one that knew the answer. The trace answers the
question directly — how much work did localization take — which is the quantity
an index would reduce, and it yields a number even for a run that never
answers.

The join is the fiddly part. `tool.proposed` carries the step and the argument
JSON; `tool.completed` carries the call id and the status but no step. A read
counts as a hit only when both halves are present and the completion says `ok`,
because a denied or not-found read never put the file in front of the model.
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
    """Events from one JSONL trace, unwrapped from their journal envelopes."""
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        events.append(record.get("event", record))
    return events


def _argument(event: dict[str, Any], name: str) -> str:
    """One argument of a proposed call. `args_summary` is the argument JSON
    clipped to 200 characters, so a long call can arrive unparseable; a path is
    near the front in practice, and an unreadable summary yields "" rather than
    an exception that would discard the whole run."""
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
    """(step, path) for every repo.read that completed successfully, in order."""
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
    """The answer key is repo-relative; a trace may hold either form."""
    normalized = read_path.replace("\\", "/").lstrip("./")
    wanted = answer.replace("\\", "/")
    return normalized == wanted or normalized.endswith("/" + wanted)


def steps_to_first_correct_read(
    events: list[dict[str, Any]], answer_files: tuple[str, ...]
) -> int | None:
    """The step at which the agent first read an answer file, or None."""
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
    """Markdown, grouped by task kind — the unique-name vs interface-impl split
    is the finding this benchmark exists to produce."""
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
