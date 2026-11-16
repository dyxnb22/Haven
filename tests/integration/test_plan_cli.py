"""跨进程可恢复的无头规划与确认执行入口。"""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.interfaces.cli import app
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import default_recipes, finish, make_repo, text, tool
from tests.integration.test_plan_mode import completed_plan_turn, edit_turn, planning_turns


@pytest.mark.parametrize("conflict", ["--write", "--worktree"])
def test_plan_rejects_conflicting_options(conflict: str) -> None:
    result = CliRunner().invoke(app, ["run", "Plan the fix", "--plan", conflict])
    assert result.exit_code == 2
    assert "cannot be combined" in result.stdout


def test_headless_plan_can_be_reviewed_then_implemented_after_reopening_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    recipe = default_recipes()["verify-calc"]
    (repo / ".haven.toml").write_text(
        "[recipes.verify-calc]\nargv = " + json.dumps(list(recipe.argv)) + "\n"
    )
    models = iter(
        [
            ScriptedModel(planning_turns()),
            ScriptedModel(
                [
                    edit_turn(),
                    [tool("diff", "repo.diff"), finish("tool_calls")],
                    [tool("check", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
                    completed_plan_turn(),
                    [text("Fixed and verified."), finish()],
                ]
            ),
        ]
    )
    build = bootstrap.build_services

    async def injected_builder(workspace: Path, **kwargs: Any) -> bootstrap.AppServices:
        return await build(
            workspace, model=next(models), store_path=tmp_path / "sessions.db", **kwargs
        )

    monkeypatch.setattr(bootstrap, "build_services", injected_builder)
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    runner = CliRunner()
    events = tmp_path / "plan-events.jsonl"
    planned = runner.invoke(
        app,
        ["run", "Fix add()", "--plan", "-w", str(repo), "--json", "--events", str(events)],
    )
    assert planned.exit_code == 0, planned.output
    plan_id = json.loads(planned.stdout)["run_id"]
    assert "return a - b" in (repo / "src/calc.py").read_text()
    updates = [json.loads(line)["event"] for line in events.read_text().splitlines()]
    assert any(e["kind"] == "plan.updated" and e["plan_status"] == "ready" for e in updates)

    implemented = runner.invoke(
        app, ["implement", plan_id, "-w", str(repo), "--approval-policy", "all", "--json"]
    )
    assert implemented.exit_code == 0, implemented.output
    outcome = json.loads(implemented.stdout)
    assert outcome["stop_reason"] == "evidence_satisfied"
    assert outcome["run_id"] != plan_id
    assert "return a + b" in (repo / "src/calc.py").read_text()
