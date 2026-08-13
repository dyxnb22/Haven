"""Run every Java localization task against the live model, read-only.

    uv run python evals/java/run_benchmark.py [--tier deep] [--only TASK_ID]

Each task is one `haven run` in read-only mode against a copy of the benchmark
repository, streaming its events to `evals/java/events/<task-id>.jsonl` for
`score.py` to grade. Read-only is the default for `haven run`, so no approval
policy is needed and the repository cannot be modified.

A run that fails still leaves its trace, which is the point: a run that died on
the budget before localizing is a finding, not a missing data point.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from evals.java.tasks import REPO_PATH, TASKS

HERE = Path(__file__).resolve().parent
EVENTS = HERE / "events"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", default="standard")
    parser.add_argument("--only", default="", help="Run a single task id.")
    args = parser.parse_args()

    repo = Path(REPO_PATH)
    if not repo.is_dir():
        print(f"benchmark copy missing: {repo}. See evals/java/README.md.")
        return 2

    EVENTS.mkdir(parents=True, exist_ok=True)
    tasks = [t for t in TASKS if not args.only or t.id == args.only]

    for index, task in enumerate(tasks, start=1):
        events_path = EVENTS / f"{task.id}.jsonl"
        started = time.monotonic()
        print(f"[{index}/{len(tasks)}] {task.id} ({task.kind}) ...", flush=True)
        completed = subprocess.run(
            [
                "uv",
                "run",
                "haven",
                "run",
                task.goal,
                "--workspace",
                str(repo),
                "--tier",
                args.tier,
                "--jsonl",
                "--events",
                str(events_path),
            ],
            capture_output=True,
            text=True,
        )
        elapsed = time.monotonic() - started
        tail = (completed.stdout or completed.stderr).strip().splitlines()
        print(f"    rc={completed.returncode} {elapsed:.0f}s {tail[-1] if tail else ''}", flush=True)

    print(f"\ntraces in {EVENTS}. Score with: uv run python evals/java/score.py {EVENTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
