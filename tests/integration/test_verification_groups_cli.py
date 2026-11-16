"""真实无头入口：选组、可写后续轮次、继承和显式清除。"""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.interfaces.cli import app
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import finish, make_repo, text
from tests.integration.test_verification_groups import check_turn, recipes


def configure(repo: Path) -> None:
    (repo / ".haven.toml").write_text(
        "[verification_groups]\nquality = ['test', 'lint']\n"
        + "".join(
            f"[recipes.{name}]\nargv = {json.dumps(list(spec.argv))}\n"
            for name, spec in recipes().items()
        )
    )


def test_cli_selection_inheritance_and_explicit_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    configure(repo)
    models = [
        ScriptedModel([check_turn("test"), check_turn("lint", "lint"), [text("Done."), finish()]]),
        ScriptedModel([]),
        ScriptedModel([check_turn("test"), check_turn("lint", "lint"), [text("Again."), finish()]]),
        ScriptedModel([[text("Only an explanation."), finish()]]),
    ]
    pending = iter(models)
    build = bootstrap.build_services

    async def injected(workspace: Path, **kwargs: Any) -> bootstrap.AppServices:
        kwargs["worktree"] = False
        return await build(
            workspace, model=next(pending), store_path=tmp_path / "sessions.db", **kwargs
        )

    monkeypatch.setattr(bootstrap, "build_services", injected)
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    runner = CliRunner()
    first = runner.invoke(
        app,
        [
            "run",
            "Verify the project",
            "-w",
            str(repo),
            "--write",
            "--approval-policy",
            "trusted-recipe",
            "--check-group",
            "quality",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    outcome = json.loads(first.stdout)
    assert outcome["verification"] == {
        "group": "quality",
        "checks": [
            {"recipe_id": "test", "status": "passed"},
            {"recipe_id": "lint", "status": "passed"},
        ],
    }
    run_id = outcome["run_id"]
    read_only = runner.invoke(app, ["continue", run_id, "Verify again", "-w", str(repo), "--json"])
    assert read_only.exit_code == 2 and "--write" in read_only.stdout
    assert models[1].requests_seen == []
    continued = runner.invoke(
        app,
        [
            "continue",
            run_id,
            "Verify again",
            "-w",
            str(repo),
            "--write",
            "--approval-policy",
            "trusted-recipe",
            "--json",
        ],
    )
    assert continued.exit_code == 0, continued.output
    follow_up = json.loads(continued.stdout)
    assert follow_up["parent_run_id"] == run_id
    assert follow_up["verification"]["group"] == "quality"
    cleared = runner.invoke(
        app,
        [
            "continue",
            follow_up["run_id"],
            "Only explain now",
            "-w",
            str(repo),
            "--clear-check-group",
            "--json",
        ],
    )
    assert cleared.exit_code == 0, cleared.output
    assert "verification" not in json.loads(cleared.stdout)
    conflict = runner.invoke(
        app,
        [
            "continue",
            run_id,
            "Explain now",
            "--check-group",
            "quality",
            "--clear-check-group",
        ],
    )
    assert conflict.exit_code == 2 and "cannot be combined" in conflict.stdout


@pytest.mark.parametrize("selection", ["quality", "unknown"])
def test_invalid_or_read_only_group_never_calls_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    repo = make_repo(tmp_path)
    configure(repo)
    model = ScriptedModel([])
    build = bootstrap.build_services

    async def injected(workspace: Path, **kwargs: Any) -> bootstrap.AppServices:
        return await build(workspace, model=model, store_path=tmp_path / "sessions.db", **kwargs)

    monkeypatch.setattr(bootstrap, "build_services", injected)
    result = CliRunner().invoke(
        app, ["run", "Verify", "-w", str(repo), "--check-group", selection, "--json"]
    )
    assert result.exit_code == 2
    assert model.requests_seen == []
    assert ("--write" if selection == "quality" else "unknown verification group") in result.stdout
