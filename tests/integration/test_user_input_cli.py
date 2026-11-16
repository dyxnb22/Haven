"""无头追问在重新打开数据库和工作树后保留未完成修改与证据。"""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.adapters.sqlite_session import SqliteSessionStore
from haven.interfaces.cli import app
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import finish, text, tool
from tests.integration.test_user_input import ask_turn
from tests.integration.test_worktree_journey import git_repo as git_repo


@pytest.mark.parametrize("review", [False, True])
def test_cli_reopens_the_waiting_worktree_and_preserves_evidence(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review: bool
) -> None:
    models = iter(
        [
            ScriptedModel(
                [
                    [tool("read", "repo.read", path="calc.py"), finish("tool_calls")],
                    [
                        tool(
                            "edit",
                            "repo.edit",
                            path="calc.py",
                            old_string="return a - b",
                            new_string="return a + b",
                        ),
                        finish("tool_calls"),
                    ],
                    ask_turn(),
                ]
            ),
            ScriptedModel(
                [
                    [tool("diff", "repo.diff"), finish("tool_calls")],
                    [tool("check", "repo.check", recipe_id="verify"), finish("tool_calls")],
                    [text("Fixed and verified."), finish()],
                ]
            ),
        ]
    )
    db = tmp_path / "questions.db"
    build = bootstrap.build_services

    async def injected_builder(workspace: Path, **kwargs: Any) -> bootstrap.AppServices:
        return await build(workspace, model=next(models), store_path=db, **kwargs)

    async def open_test_store() -> SqliteSessionStore:
        return await SqliteSessionStore.open(db, bootstrap.artifacts_dir())

    monkeypatch.setattr(bootstrap, "build_services", injected_builder)
    monkeypatch.setattr(bootstrap, "open_store", open_test_store)
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    runner = CliRunner()
    paused = runner.invoke(
        app,
        [
            "run",
            "Fix add()",
            "-w",
            str(git_repo),
            "--write",
            "--worktree",
            *(["--review"] if review else []),
            "--approval-policy",
            "all",
            "--json",
        ],
    )
    assert paused.exit_code == 8, paused.output
    waiting = json.loads(paused.stdout)
    assert waiting["stop_reason"] == "needs_input"
    worktrees = list((git_repo / ".haven/worktrees").iterdir())
    assert len(worktrees) == 1
    assert "return a + b" in (worktrees[0] / "calc.py").read_text()
    assert "return a - b" in (git_repo / "calc.py").read_text()

    resumed = runner.invoke(
        app,
        [
            "answer",
            waiting["run_id"],
            "-w",
            str(git_repo),
            "--request",
            waiting["input_request"]["request_id"],
            "--answers",
            '{"scope":"Parser"}',
            "--write",
            "--approval-policy",
            "all",
            "--json",
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    outcome = json.loads(resumed.stdout)
    assert outcome["run_id"] == waiting["run_id"]
    assert outcome["stop_reason"] == "evidence_satisfied"
    if review:
        assert outcome["review_required"]
        assert "return a - b" in (git_repo / "calc.py").read_text()
        resolved = runner.invoke(
            app,
            [
                "review",
                outcome["run_id"],
                "-w",
                str(git_repo),
                "--apply",
                "--binding",
                outcome["review_binding"],
            ],
        )
        assert resolved.exit_code == 0, resolved.output
    assert "return a + b" in (git_repo / "calc.py").read_text()
    assert not worktrees[0].exists()
