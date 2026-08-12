"""Drive each coding agent over the exported head-to-head cases.

Each driver takes a prepared, git-initialised buggy checkout plus the goal and
lets that tool edit the tree in place. Nothing here scores anything — the
neutral grader in harness.py does that afterward by running the project's own
verify recipe and reading git diff, identically for every tool.

    uv run python evals/headtohead/drivers.py --tool haven --subset default
    uv run python evals/headtohead/drivers.py --tool codex --subset default

Both drivers point the model at the same DeepSeek endpoint via the same env
vars Haven's own live eval uses, so the only variable is the harness.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness import _BY_ID, SUBSETS, _prepare_repo, _spec  # noqa: E402

MODEL = os.environ.get("HAVEN_MODEL", "deepseek-v4-flash")
BASE_URL = os.environ.get("HAVEN_BASE_URL", "https://api.deepseek.com/v1")


def _write_haven_config(repo: Path, verify_argv: tuple[str, ...]) -> None:
    argv = ", ".join(f'"{a}"' for a in verify_argv)
    (repo / ".haven.toml").write_text(f"[recipes.verify]\nargv = [{argv}]\n", encoding="utf-8")


def drive_haven(subset: str) -> None:
    """Haven in headless auto-fix mode: the same channel a user gets, scored
    afterward by the neutral grader rather than by Haven's own gate."""
    for case_id in SUBSETS[subset]:
        task = _BY_ID[case_id]
        spec = _spec(task)
        repo = _prepare_repo(task, "haven", subset)
        _write_haven_config(repo, spec.verify_argv)
        started = time.monotonic()
        proc = subprocess.run(
            [
                "uv",
                "run",
                "haven",
                "run",
                spec.goal,
                "--workspace",
                str(repo),
                "--write",
                "--approval-policy",
                "all",
                "--jsonl",
            ],
            cwd=HERE.parent.parent,
            capture_output=True,
            text=True,
            env={**os.environ},
        )
        elapsed = time.monotonic() - started
        (repo.parent / "driver.json").write_text(
            json.dumps(
                {
                    "tool": "haven",
                    "elapsed_s": round(elapsed, 1),
                    "returncode": proc.returncode,
                    "stdout_tail": proc.stdout[-2000:],
                },
                indent=2,
            )
        )
        print(f"haven/{case_id}: rc={proc.returncode} in {elapsed:.0f}s")


#: DeepSeek as an OpenAI-compatible provider, injected via `codex exec -c`
#: overrides so no global ~/.codex/config.toml edit is needed.
_CODEX_PROVIDER = [
    "-c",
    'model_providers.deepseek.name="DeepSeek"',
    "-c",
    f'model_providers.deepseek.base_url="{BASE_URL}"',
    "-c",
    'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"',
    "-c",
    'model_providers.deepseek.wire_api="chat"',
    "-c",
    'model_provider="deepseek"',
]


def drive_codex(subset: str) -> None:
    for case_id in SUBSETS[subset]:
        task = _BY_ID[case_id]
        spec = _spec(task)
        repo = _prepare_repo(task, "codex", subset)
        prompt = (
            f"{spec.goal}\n\n"
            "Work only inside this repository. When done, ensure the project's "
            "test suite passes."
        )
        started = time.monotonic()
        proc = subprocess.run(
            [
                "codex",
                "exec",
                "-C",
                str(repo),
                "-m",
                MODEL,
                *_CODEX_PROVIDER,
                "--dangerously-bypass-approvals-and-sandbox",
                prompt,
            ],
            capture_output=True,
            text=True,
            env={**os.environ},
        )
        elapsed = time.monotonic() - started
        (repo.parent / "driver.json").write_text(
            json.dumps(
                {
                    "tool": "codex",
                    "elapsed_s": round(elapsed, 1),
                    "returncode": proc.returncode,
                    "stdout_tail": proc.stdout[-2000:],
                    "stderr_tail": proc.stderr[-2000:],
                },
                indent=2,
            )
        )
        print(f"codex/{case_id}: rc={proc.returncode} in {elapsed:.0f}s")


def drive_opencode(subset: str) -> None:
    """opencode in non-interactive `run` mode against the same DeepSeek model,
    editing the checkout in place. Scored afterward by the neutral grader."""
    for case_id in SUBSETS[subset]:
        task = _BY_ID[case_id]
        spec = _spec(task)
        repo = _prepare_repo(task, "opencode", subset)
        prompt = (
            f"{spec.goal}\n\nWork only inside this repository and make the project's tests pass."
        )
        started = time.monotonic()
        proc = subprocess.run(
            [
                "opencode",
                "run",
                "--dir",
                str(repo),
                "-m",
                f"deepseek/{MODEL}",
                prompt,
            ],
            capture_output=True,
            text=True,
            env={**os.environ},
        )
        elapsed = time.monotonic() - started
        (repo.parent / "driver.json").write_text(
            json.dumps(
                {
                    "tool": "opencode",
                    "elapsed_s": round(elapsed, 1),
                    "returncode": proc.returncode,
                    "stdout_tail": proc.stdout[-2000:],
                    "stderr_tail": proc.stderr[-2000:],
                },
                indent=2,
            )
        )
        print(f"opencode/{case_id}: rc={proc.returncode} in {elapsed:.0f}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True, choices=("haven", "codex", "opencode"))
    parser.add_argument("--subset", default="default")
    args = parser.parse_args()
    if args.tool == "haven":
        drive_haven(args.subset)
    elif args.tool == "opencode":
        drive_opencode(args.subset)
    else:
        drive_codex(args.subset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
