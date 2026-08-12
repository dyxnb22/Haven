"""Generate live real-repo fixtures and case JSON from tasks.py.

    uv run python evals/real/build.py            # build fixtures + cases
    uv run python evals/real/build.py --verify   # also prove each task is
                                                 # red with the bug and green
                                                 # when the injection is reverted

Fixtures and cases are written under evals/real/{fixtures,cases} and are not
committed (see .gitignore) — they are large third-party copies. Only tasks.py,
this script, and repos.lock are tracked, so the suite is reproducible from the
pinned commits.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOS_DIR = HERE / "repos"
FIXTURES_DIR = HERE / "fixtures"
CASES_DIR = HERE / "cases"

sys.path.insert(0, str(HERE))
from tasks import REPOS, TASKS, Repo, Task  # noqa: E402

_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", ".tox")


def _apply(text: str, old: str, new: str, where: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{where}: snippet must appear exactly once, found {count}:\n  {old!r}")
    return text.replace(old, new)


def _materialize(
    task: Task, repo: Repo, dest: Path, *, reverted: bool, write_conftest: bool = True
) -> None:
    """Copy the clone to dest and apply (or, if reverted, do not apply) the bug."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REPOS_DIR / repo.dir, dest, ignore=_IGNORE)
    for rel, old, new in task.inject:
        target = dest / rel
        text = target.read_text(encoding="utf-8")
        if reverted:
            # The clean tree is the "new" side of the injection already; only a
            # bug-injected tree needs reverting. Nothing to do here.
            continue
        target.write_text(_apply(text, old, new, f"{task.id}:{rel}"), encoding="utf-8")
    if repo.src_path and write_conftest:
        # src-layout projects are not installed in the fixture; a root conftest
        # puts the package on sys.path so `python -m pytest` can import it.
        # Zero-config fixtures skip this: the shim is pre-configuration, and the
        # discovered command must solve the import path on its own.
        conftest = dest / "conftest.py"
        conftest.write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, str(pathlib.Path(__file__).parent / {repo.src_path!r}))\n",
            encoding="utf-8",
        )


def _case_json(task: Task, repo: Repo) -> dict:
    argv = ["{python}", "-m", "pytest", *repo.verify]
    return {
        "id": task.id,
        "category": "real",
        "goal": task.goal,
        "fixture": task.id,
        "approval_policy": "approve_all",
        "recipes": {"verify": {"argv": argv, "timeout_seconds": repo.timeout}},
        "expect": {
            "status": "succeeded",
            "allowed_changed_files": list(task.allowed),
        },
    }


#: Zero-config variants: one task per repo, no authored recipe, no conftest
#: shim — the runner registers whatever `discover_recipes` proposes, measuring
#: the discovery loop end-to-end. Expectations follow the measured hit rate:
#: wcwidth's own tox.ini addopts demand pytest-cov (absent here), so its
#: discovered check can never pass and the honest outcome is a stop.
ZEROCONF: list[tuple[str, str]] = [
    ("jmespath-starts-with", "succeeded"),
    ("idna-label-length", "succeeded"),
    ("wcwidth-ascii", "stopped"),
    ("tomli-tz-sign", "succeeded"),
    ("tabulate-padleft", "succeeded"),
]


def _zeroconf_case_json(task: Task, expect_status: str) -> dict:
    return {
        "id": f"zeroconf-{task.id}",
        "category": "real",
        "goal": task.goal.replace("the `verify` check", "the project's tests"),
        "fixture": f"zeroconf-{task.id}",
        "approval_policy": "approve_all",
        "discover": True,
        "expect": {
            "status": expect_status,
            "allowed_changed_files": list(task.allowed),
        },
    }


def build() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    for task in TASKS:
        repo = REPOS[task.repo]
        _materialize(task, repo, FIXTURES_DIR / task.id, reverted=False)
        (CASES_DIR / f"{task.id}.json").write_text(
            json.dumps(_case_json(task, repo), indent=2), encoding="utf-8"
        )
    by_id = {task.id: task for task in TASKS}
    for task_id, expect_status in ZEROCONF:
        task = by_id[task_id]
        repo = REPOS[task.repo]
        _materialize(
            task, repo, FIXTURES_DIR / f"zeroconf-{task.id}", reverted=False, write_conftest=False
        )
        (CASES_DIR / f"zeroconf-{task.id}.json").write_text(
            json.dumps(_zeroconf_case_json(task, expect_status), indent=2), encoding="utf-8"
        )
    print(
        f"built {len(TASKS)} fixtures + {len(ZEROCONF)} zero-config variants "
        f"in {FIXTURES_DIR} and cases in {CASES_DIR}"
    )


def _run_suite(repo: Repo, cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *repo.verify],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=repo.timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr).splitlines()[-1] if proc.stdout else ""


def verify() -> int:
    """Prove every task is a well-formed bug: red as injected, green as reverted."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="haven-real-verify-") as tmp:
        root = Path(tmp)
        for task in TASKS:
            repo = REPOS[task.repo]
            # Injected: apply the bug into a clean copy → suite must be RED.
            buggy = root / f"{task.id}-buggy"
            _materialize(task, repo, buggy, reverted=False)
            rc_bug, tail_bug = _run_suite(repo, buggy)
            # Reverted: the clean clone → suite must be GREEN.
            clean = root / f"{task.id}-clean"
            _materialize(task, repo, clean, reverted=True)
            rc_clean, tail_clean = _run_suite(repo, clean)

            ok = rc_bug != 0 and rc_clean == 0
            status = "ok" if ok else "BAD"
            print(f"[{status}] {task.id}: bug rc={rc_bug} ({tail_bug}) | clean rc={rc_clean}")
            if not ok:
                failures.append(task.id)
    if failures:
        print(f"\n{len(failures)} malformed task(s): {', '.join(failures)}")
        return 1
    print(f"\nall {len(TASKS)} tasks are red-with-bug and green-when-reverted")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="check each task is red/green")
    args = parser.parse_args()
    build()
    if args.verify:
        return verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
