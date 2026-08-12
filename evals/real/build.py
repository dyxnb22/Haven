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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from haven.adapters.process_executor import ENV_ALLOWLIST, RECIPE_SCRATCH_DIRNAME
from haven.bootstrap import select_launcher
from haven.ports.sandbox import SandboxSpec, default_private_roots, default_readable_roots

HERE = Path(__file__).resolve().parent
REPOS_DIR = HERE / "repos"
FIXTURES_DIR = HERE / "fixtures"
CASES_DIR = HERE / "cases"

sys.path.insert(0, str(HERE))
from tasks import REFACTORS, REPOS, TASKS, RefactorTask, Repo, Task  # noqa: E402

# .venv: uv creates one inside a clone if `uv run` is ever invoked there (the
# larger tier-3 repos ship their own pyproject/uv.lock); copying it into every
# fixture would be huge and meaningless.
_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", ".tox", ".venv")


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


#: Tiers on 10k+ line repositories get a doubled input-token ceiling. The
#: default 400k (ADR 0006) was derived on small trees; measured tier-3
#: trajectories legitimately reach 17-23 steps at ~20k tokens/step, so the
#: default cap truncates the slow tail of otherwise-correct runs (observed
#: live: 3/20 in one run). The step/tool/wall budgets are unchanged — this
#: calibrates a resource ceiling to repo scale, it does not ease the task.
_BIG_REPO_TIERS = ("tier3", "tier4")


def _case_json(task: Task | RefactorTask, repo: Repo) -> dict:
    argv = ["{python}", "-m", "pytest", *repo.verify]
    case = {
        "id": task.id,
        "category": "real",
        "goal": task.goal,
        "fixture": task.id,
        "approval_policy": "approve_all",
        "recipes": {"verify": {"argv": argv, "timeout_seconds": repo.timeout}},
        # The hidden grader: the harness reruns `verify` on the final tree
        # after the agent finishes. An agent that answers its way to
        # "succeeded" without fixing anything fails here (found live: a
        # tier-4 bug-fix case passed with zero edits before this existed).
        "hidden_check": "verify",
        "expect": {
            "status": "succeeded",
            "allowed_changed_files": list(task.allowed),
        },
    }
    if any(tag in task.tags for tag in _BIG_REPO_TIERS):
        case["budget"] = {"max_input_tokens": 800_000}
    return case


def _materialize_refactor(task: RefactorTask, repo: Repo, dest: Path, *, reference: bool) -> None:
    """Clean clone + the task test; with `reference`, the solution applied."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REPOS_DIR / repo.dir, dest, ignore=_IGNORE)
    for rel, content in task.add_files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if reference:
        for rel, old, new in task.reference_edits:
            target = dest / rel
            text = target.read_text(encoding="utf-8")
            target.write_text(_apply(text, old, new, f"{task.id}:{rel}"), encoding="utf-8")
        for rel, content in task.reference_creates:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    if repo.src_path:
        conftest = dest / "conftest.py"
        conftest.write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, str(pathlib.Path(__file__).parent / {repo.src_path!r}))\n",
            encoding="utf-8",
        )


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
    for refactor in REFACTORS:
        repo = REPOS[refactor.repo]
        _materialize_refactor(refactor, repo, FIXTURES_DIR / refactor.id, reference=False)
        (CASES_DIR / f"{refactor.id}.json").write_text(
            json.dumps(_case_json(refactor, repo), indent=2), encoding="utf-8"
        )
    _write_subset("tier3", [task for task in TASKS if "tier3" in task.tags])
    _write_subset(
        "tier4",
        [task for task in TASKS if "tier4" in task.tags] + list(REFACTORS),
    )
    _write_subset("tier5", [task for task in TASKS if "tier5" in task.tags])
    print(
        f"built {len(TASKS)} + {len(REFACTORS)} fixtures + {len(ZEROCONF)} zero-config "
        f"variants in {FIXTURES_DIR} and cases in {CASES_DIR}"
    )


def _write_subset(name: str, tasks: list[Task | RefactorTask]) -> None:
    """A sibling run-dir (cases + a fixtures symlink) so `haven eval --cases`
    can run just this slice; the runner resolves fixtures as a sibling of the
    cases directory. Same layout the zeroconf/ and tier2/ dirs use."""
    subset = HERE / name
    (subset / "cases").mkdir(parents=True, exist_ok=True)
    for stale in (subset / "cases").glob("*.json"):
        stale.unlink()
    link = subset / "fixtures"
    if not link.is_symlink():
        link.symlink_to("../fixtures")
    for task in tasks:
        repo = REPOS[task.repo]
        (subset / "cases" / f"{task.id}.json").write_text(
            json.dumps(_case_json(task, repo), indent=2), encoding="utf-8"
        )


#: The same backend the live eval's executor will select. Verification must
#: run the suite the way `repo.check` will, or it proves the wrong thing.
_LAUNCHER = select_launcher()


def _run_suite(repo: Repo, cwd: Path) -> tuple[int, str]:
    """Run the verify command exactly as a registered check would run it:
    wrapped by the OS sandbox and given the executor's scrubbed environment.

    Tier 3 taught this the hard way: click's suite is green when run raw but
    red inside the sandbox (a pager test kills its child process, which
    Seatbelt denies), so every click case burned its budget against an oracle
    that could never pass. Red/green proven through the real confinement
    catches that class before any model is called.
    """
    argv: tuple[str, ...] = (sys.executable, "-m", "pytest", *repo.verify)
    scratch = cwd / RECIPE_SCRATCH_DIRNAME
    scratch.mkdir(parents=True, exist_ok=True)
    if _LAUNCHER is not None:
        argv = _LAUNCHER.wrap(
            argv,
            SandboxSpec(
                workspace_root=cwd,
                scratch_dir=scratch,
                writable=True,
                private_roots=default_private_roots(),
                extra_readable_roots=default_readable_roots(),
            ),
        )
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env["TMPDIR"] = str(scratch)
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=repo.timeout,
        env=env,
    )
    return proc.returncode, (proc.stdout + proc.stderr).splitlines()[-1] if proc.stdout else ""


def verify(only: str = "") -> int:
    """Prove every task is well-formed before any model is called.

    Injections: red with the bug, green when reverted. Refactors: red as
    built (the task test pins a shape that does not exist yet), green with
    the committed reference solution applied — the same discipline, pointed
    the other way. Honesty tasks have no injection: their claim was manually
    verified false and the clean-clone green is already proven by every
    other task on the same repo, so they are skipped here.
    """
    failures: list[str] = []
    tasks = [task for task in TASKS if only in task.id]
    refactors = [task for task in REFACTORS if only in task.id]
    with tempfile.TemporaryDirectory(prefix="haven-real-verify-") as tmp:
        root = Path(tmp)
        for task in tasks:
            repo = REPOS[task.repo]
            if not task.inject:
                print(f"[skip] {task.id}: honesty task (no injection to prove)")
                continue
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
        for refactor in refactors:
            repo = REPOS[refactor.repo]
            as_built = root / f"{refactor.id}-asbuilt"
            _materialize_refactor(refactor, repo, as_built, reference=False)
            rc_red, tail_red = _run_suite(repo, as_built)
            solved = root / f"{refactor.id}-reference"
            _materialize_refactor(refactor, repo, solved, reference=True)
            rc_green, tail_green = _run_suite(repo, solved)

            ok = rc_red != 0 and rc_green == 0
            status = "ok" if ok else "BAD"
            print(
                f"[{status}] {refactor.id}: as-built rc={rc_red} ({tail_red}) | "
                f"reference rc={rc_green} ({tail_green})"
            )
            if not ok:
                failures.append(refactor.id)
    if failures:
        print(f"\n{len(failures)} malformed task(s): {', '.join(failures)}")
        return 1
    print(f"\nall {len(tasks) + len(refactors)} verified tasks are well-formed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="check each task is red/green")
    parser.add_argument(
        "--only",
        default="",
        help="verify only tasks whose id contains this substring (build stays full)",
    )
    args = parser.parse_args()
    build()
    if args.verify:
        return verify(args.only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
