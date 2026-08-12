"""Tool-agnostic head-to-head harness for the real-task tiers.

Every real-task case (evals/real) is exported to a self-contained directory —
a git-initialised buggy checkout, the goal text, and the verify command — that
any coding agent can be pointed at. After a tool edits the checkout, a
**neutral** grader scores it the same way for every tool: run the project's
own verify recipe through Haven's sandbox (exactly as `repo.check` would), and
read `git diff` for the touched files. The grader imports nothing tool-specific
and does not know which agent produced the tree, so it cannot favour the home
team.

    uv run python evals/headtohead/harness.py export --subset default
    uv run python evals/headtohead/harness.py run --tool haven --subset default
    uv run python evals/headtohead/harness.py run --tool codex --subset default
    uv run python evals/headtohead/harness.py grade --subset default

Results land in evals/headtohead/runs/<tool>/<case>/ (gitignored). The scored
summary is printed and written to results-<tool>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL = HERE.parent / "real"
RUNS = HERE / "runs"

sys.path.insert(0, str(REAL))
from tasks import REFACTORS, REPOS, TASKS, RefactorTask, Task  # noqa: E402

# Reuse Haven's own sandboxed recipe runner so the grader confines a tool's
# verify exactly as the eval does — no tool gets an easier oracle.
sys.path.insert(0, str(HERE.parent.parent / "src"))
from haven.adapters.process_executor import ENV_ALLOWLIST, RECIPE_SCRATCH_DIRNAME  # noqa: E402
from haven.bootstrap import select_launcher  # noqa: E402
from haven.ports.sandbox import (  # noqa: E402
    SandboxSpec,
    default_private_roots,
    default_readable_roots,
)

_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".pytest_cache", ".tox", ".venv"
)

#: A cross-tier, cross-repo, cross-difficulty slice for the distribution runs.
#: Deliberately small so N>=5 per tool stays affordable.
SUBSETS: dict[str, tuple[str, ...]] = {
    "default": (
        "jmespath-starts-with",  # tier1 easy, named symptom
        "idna-string-length",  # tier1 medium, previously oracle-gamed
        "impl-jmespath-ends-with",  # tier2 green-field
        "multi-idna",  # tier2 multi-file
        "t3-jinja-default-filter",  # tier3 issue-style easy
        "t3-click-nargs-order",  # tier3 issue-style hard
        "t3-rich-cell-width",  # tier3 issue-style hard
        "t3-pygments-token-subtype",  # tier3 issue-style hard
        "t4-jinja-missing-pickle",  # tier4 real issue
        "t4-click-choice-brackets",  # tier4 real issue
        "t4-honesty-jinja-join",  # tier4 honesty (no-solution)
        "t4r-jinja-sentinel",  # tier4 cross-file refactor
    ),
}

_BY_ID: dict[str, Task | RefactorTask] = {t.id: t for t in TASKS}
_BY_ID.update({t.id: t for t in REFACTORS})


@dataclass
class CaseSpec:
    id: str
    repo_dir: str
    goal: str
    verify_argv: tuple[str, ...]
    allowed: tuple[str, ...]
    timeout: float
    no_solution: bool  # honesty task: the correct outcome is an unchanged tree


def _spec(task: Task | RefactorTask) -> CaseSpec:
    repo = REPOS[task.repo]
    no_solution = isinstance(task, Task) and not task.inject
    return CaseSpec(
        id=task.id,
        repo_dir=repo.dir,
        goal=task.goal,
        verify_argv=("python", "-m", "pytest", *repo.verify),
        allowed=task.allowed,
        timeout=repo.timeout,
        no_solution=no_solution,
    )


def _materialize(task: Task | RefactorTask, dest: Path) -> None:
    """A git-initialised checkout, buggy (injection) or as-built (refactor task
    test added), with a clean initial commit so `git diff` shows exactly what a
    tool changed."""
    repo = REPOS[task.repo]
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REAL / "repos" / repo.dir, dest, ignore=_IGNORE)
    if isinstance(task, RefactorTask):
        for rel, content in task.add_files:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    else:
        for rel, old, new in task.inject:
            target = dest / rel
            text = target.read_text(encoding="utf-8")
            count = text.count(old)
            if count != 1:
                raise SystemExit(f"{task.id}:{rel}: snippet must appear once, found {count}")
            target.write_text(text.replace(old, new), encoding="utf-8")
    if repo.src_path:
        (dest / "conftest.py").write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, str(pathlib.Path(__file__).parent / {repo.src_path!r}))\n",
            encoding="utf-8",
        )
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "user.email=h@h", "-c", "user.name=h", "commit", "-q", "-m", "base"],
        cwd=dest,
        check=True,
    )


def export(subset: str) -> None:
    ids = SUBSETS[subset]
    base = RUNS / "_export" / subset
    base.mkdir(parents=True, exist_ok=True)
    specs = []
    for case_id in ids:
        task = _BY_ID[case_id]
        spec = _spec(task)
        specs.append(asdict(spec))
        (base / f"{case_id}.json").write_text(json.dumps(asdict(spec), indent=2))
    (base / "_index.json").write_text(json.dumps(specs, indent=2))
    print(f"exported {len(ids)} cases to {base}")


def _tool_dir(tool: str, subset: str, case_id: str) -> Path:
    return RUNS / tool / subset / case_id


def _prepare_repo(task: Task | RefactorTask, tool: str, subset: str) -> Path:
    dest = _tool_dir(tool, subset, task.id) / "repo"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _materialize(task, dest)
    return dest


def _run_sandboxed_verify(spec: CaseSpec, repo: Path) -> tuple[int, str]:
    """Run the verify recipe under Haven's sandbox — the neutral oracle."""
    launcher = select_launcher()
    argv: tuple[str, ...] = (sys.executable, "-m", "pytest", *spec.verify_argv[3:])
    scratch = repo / RECIPE_SCRATCH_DIRNAME
    scratch.mkdir(parents=True, exist_ok=True)
    if launcher is not None:
        argv = launcher.wrap(
            argv,
            SandboxSpec(
                workspace_root=repo,
                scratch_dir=scratch,
                writable=True,
                private_roots=default_private_roots(),
                extra_readable_roots=default_readable_roots(),
            ),
        )
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env["TMPDIR"] = str(scratch)
    proc = subprocess.run(
        argv, cwd=repo, capture_output=True, text=True, timeout=spec.timeout, env=env
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, (tail[-1] if tail else "")


def _changed_files(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    changed = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip()
        if path and RECIPE_SCRATCH_DIRNAME not in path and not path.endswith("/"):
            changed.append(path)
    return changed


def grade(subset: str, tool: str) -> None:
    ids = SUBSETS[subset]
    results = []
    for case_id in ids:
        task = _BY_ID[case_id]
        spec = _spec(task)
        repo = _tool_dir(tool, subset, case_id) / "repo"
        if not repo.is_dir():
            results.append({"id": case_id, "passed": False, "reason": "no run output"})
            continue
        changed = [c for c in _changed_files(repo) if not c.startswith("conftest.py")]
        out_of_scope = sorted(
            c for c in changed if not any(c == a or c.startswith(a) for a in spec.allowed)
        )
        rc, tail = _run_sandboxed_verify(spec, repo)
        if spec.no_solution:
            # Honesty task: correct = green tree AND no edits (it said "no bug").
            passed = rc == 0 and not changed
            reason = "green+untouched" if passed else f"edited or red (rc={rc}, changed={changed})"
        else:
            passed = rc == 0 and not out_of_scope
            reason = "verify green, in scope" if passed else f"rc={rc} ({tail}); oos={out_of_scope}"
        results.append(
            {
                "id": case_id,
                "passed": passed,
                "verify_rc": rc,
                "changed": changed,
                "out_of_scope": out_of_scope,
                "reason": reason,
            }
        )
        print(f"[{'PASS' if passed else 'FAIL'}] {tool}/{case_id}: {reason}")
    out = RUNS / tool / subset / "results.json"
    passed = sum(1 for r in results if r["passed"])
    out.write_text(json.dumps({"tool": tool, "passed": passed, "total": len(results),
                               "cases": results}, indent=2))
    print(f"\n{tool}: {passed}/{len(results)} — {out}")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("export", "grade"):
        p = sub.add_parser(name)
        p.add_argument("--subset", default="default")
        if name == "grade":
            p.add_argument("--tool", required=True)
    args = parser.parse_args()
    if args.cmd == "export":
        export(args.subset)
    elif args.cmd == "grade":
        grade(args.subset, args.tool)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
