"""审阅快照绑定实际内容；合流拒绝漂移并能回滚部分写入。"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.adapters.sqlite_session import SqliteSessionStore
from haven.adapters.workspace_fs import FsWorkspace
from haven.adapters.worktree import EphemeralWorktree, reconcile_worktree_to_main
from haven.application.diff_review import ReviewService, parse_diff
from haven.domain.enums import RunStatus
from haven.interfaces.cli import app
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import Harness, finish, make_repo, text, tool
from tests.integration.test_worktree_journey import git_repo as git_repo


def edit_turns(path: str = "src/calc.py", recipe: str = "verify-calc") -> list[list[Any]]:
    return [
        [tool("read", "repo.read", path=path), finish("tool_calls")],
        [
            tool(
                "edit",
                "repo.edit",
                path=path,
                old_string="return a - b",
                new_string="return a + b",
                occurrence=1,
            ),
            finish("tool_calls"),
        ],
        [tool("diff", "repo.diff"), finish("tool_calls")],
        [tool("check", "repo.check", recipe_id=recipe), finish("tool_calls")],
        [text("fixed and verified"), finish()],
    ]


async def setup_review(tmp_path: Path) -> tuple[ReviewService, Harness, Path, str]:
    repo = make_repo(tmp_path)
    main = tmp_path / "main"
    shutil.copytree(repo, main)
    harness = Harness(repo, edit_turns())
    outcome = await harness.service.run("fix add", require_review=True)
    assert outcome.status is RunStatus.SUCCEEDED
    checkpoint = await harness.store.load_checkpoint(outcome.run_id)
    assert checkpoint is not None and checkpoint.evidence.edits
    worktree = EphemeralWorktree(repo_root=main, worktree_path=repo, run_id=outcome.run_id)

    async def apply(
        paths: tuple[str, ...], before: dict[str, str], after: dict[str, str]
    ) -> list[str]:
        return await reconcile_worktree_to_main(
            worktree, paths, expected_preimages=before, expected_postimages=after
        )

    return (
        ReviewService(harness.store, harness.workspace, FsWorkspace(main), apply),
        harness,
        main,
        outcome.run_id,
    )


async def test_review_binding_applies_only_run_files_once(tmp_path: Path) -> None:
    review, harness, main, run_id = await setup_review(tmp_path)
    with pytest.raises(ValueError, match="review"):
        await harness.service.run("start another task")
    (main / "README.md").write_text("independent developer edit\n")
    snapshot = await review.prepare(run_id)
    assert snapshot.files == ("src/calc.py",)
    assert "return a - b" in (main / "src/calc.py").read_text()
    with pytest.raises(ValueError, match="stale"):
        await review.apply(run_id, "wrong")
    assert await review.apply(run_id, snapshot.binding) == ["src/calc.py"]
    assert "return a + b" in (main / "src/calc.py").read_text()
    assert (main / "README.md").read_text() == "independent developer edit\n"
    checkpoint = await harness.store.load_checkpoint(run_id)
    assert checkpoint is not None and checkpoint.review is not None and checkpoint.review.applied
    with pytest.raises(ValueError, match="already applied"):
        await review.apply(run_id, snapshot.binding)


@pytest.mark.parametrize("side", ["main", "worktree"])
async def test_review_refuses_edits_after_verification_or_review(tmp_path: Path, side: str) -> None:
    review, harness, main, run_id = await setup_review(tmp_path)
    snapshot = await review.prepare(run_id)
    target = main if side == "main" else harness.workspace.root
    (target / "src/calc.py").write_text("concurrent change\n")
    with pytest.raises(ValueError, match="changed"):
        await review.apply(run_id, snapshot.binding)
    assert "return a + b" not in (main / "src/calc.py").read_text()
    checkpoint = await harness.store.load_checkpoint(run_id)
    assert (
        checkpoint is not None and checkpoint.review is not None and not checkpoint.review.applied
    )


async def test_reconciliation_rolls_back_on_second_file_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, local = tmp_path / "main", tmp_path / "local"
    main.mkdir()
    local.mkdir()
    for name in ("a.txt", "b.txt"):
        (main / name).write_text("before")
        (local / name).write_text("after")
    replace = Path.replace

    def fail_second(source: Path, target: Path) -> Path:
        if source.name.startswith(".haven-reconcile-") and target.name == "b.txt":
            raise OSError("injected write failure")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    with pytest.raises(OSError, match="injected"):
        await reconcile_worktree_to_main(EphemeralWorktree(main, local, "run"), ["a.txt", "b.txt"])
    assert [(main / name).read_text() for name in ("a.txt", "b.txt")] == ["before", "before"]
    assert sorted(p.name for p in main.iterdir()) == ["a.txt", "b.txt"]


async def test_fresh_run_has_its_own_diff_baseline(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    harness = Harness(
        repo,
        [
            *edit_turns(),
            [tool("read2", "repo.read", path="README.md"), finish("tool_calls")],
            [
                tool(
                    "edit2",
                    "repo.edit",
                    path="README.md",
                    old_string="Calc demo",
                    new_string="Calculator",
                ),
                finish("tool_calls"),
            ],
            [tool("diff2", "repo.diff"), finish("tool_calls")],
            [tool("check2", "repo.check", recipe_id="always-pass"), finish("tool_calls")],
            [text("updated readme"), finish()],
        ],
    )
    assert (await harness.service.run("fix add")).status is RunStatus.SUCCEEDED
    second = await harness.service.run("update readme")
    assert second.status is RunStatus.SUCCEEDED
    diff = await harness.service.get_diff(second.run_id)
    assert "README.md" in diff
    assert "src/calc.py" not in diff
    assert "return a + b" in (repo / "src/calc.py").read_text()


def test_diff_parser_ignores_headers_inside_hunks() -> None:
    diff = (
        "--- a/real.txt\n+++ b/real.txt\n@@ -1 +1 @@\n--- a/fake.txt\n+++ b/fake.txt\n"
        "@@ -5 +5 @@\n-old\n+new\n"
        "--- a/deleted.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    )
    files = parse_diff(diff)
    assert [f.path for f in files] == ["real.txt", "deleted.txt"]
    assert len(files[0].hunks) == 2
    assert "+new" in files[0].hunks[1]


@pytest.mark.parametrize("resolution", ["apply", "discard"])
def test_cli_review_survives_restart_and_resolves_without_model(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolution: str
) -> None:
    db = tmp_path / "review.db"
    build = bootstrap.build_services
    builds = []

    async def builder(workspace: Path, **kwargs: Any) -> bootstrap.AppServices:
        builds.append(workspace)
        return await build(
            workspace, model=ScriptedModel(edit_turns("calc.py", "verify")), store_path=db, **kwargs
        )

    async def open_store() -> SqliteSessionStore:
        return await SqliteSessionStore.open(db, bootstrap.artifacts_dir())

    monkeypatch.setattr(bootstrap, "build_services", builder)
    monkeypatch.setattr(bootstrap, "open_store", open_store)
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "fix add",
            "-w",
            str(git_repo),
            "--write",
            "--worktree",
            "--review",
            "--approval-policy",
            "all",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.stdout)
    assert outcome["review_required"]
    run_id = outcome["run_id"]
    worktrees = list((git_repo / ".haven/worktrees").iterdir())
    assert len(worktrees) == 1
    assert "return a - b" in (git_repo / "calc.py").read_text()
    base = ["review", run_id, "-w", str(git_repo)]
    view = runner.invoke(app, [*base, "--file", "calc.py", "--hunk", "1"])
    assert view.exit_code == 0, view.output
    assert "+    return a + b" in view.stdout
    binding = outcome["review_binding"]
    denied = runner.invoke(app, [*base, f"--{resolution}", "--binding", "wrong"])
    assert denied.exit_code == 2, denied.output
    resolved = runner.invoke(app, [*base, f"--{resolution}", "--binding", binding])
    assert resolved.exit_code == 0, resolved.output
    assert not worktrees[0].exists()
    assert ("return a + b" in (git_repo / "calc.py").read_text()) == (resolution == "apply")
    assert len(builds) == 1
