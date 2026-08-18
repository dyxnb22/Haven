"""以只读方式针对实时模型运行每个 Java 定位任务。

    uv run python -m evals.java.run_benchmark [--tier deep] [--only TASK_ID]

请以模块而不是路径运行：任务和答案键从 `evals.java` 导入，只有仓库根目录是 cwd
时它才位于导入路径中。

每个任务都会在基准仓库的副本上以只读模式执行一次 `haven run`，并将事件流写入
`evals/java/events/<task-id>.jsonl` 供 `score.py` 评分。`haven run` 默认只读，
因此不需要审批策略，仓库也不会被修改。

即使运行失败，其追踪记录仍会留下，这正是目的：在完成定位前因预算停止的运行是
一个发现，而不是缺失的数据点。
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
        last = tail[-1] if tail else ""
        print(f"    rc={completed.returncode} {elapsed:.0f}s {last}", flush=True)

    print(f"\ntraces in {EVENTS}. Score with: uv run python -m evals.java.score {EVENTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
