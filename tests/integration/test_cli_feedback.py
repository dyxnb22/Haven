"""在真实无头入口验证人类提示、终端响铃与机器输出的隔离。"""

import json
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.contracts.model import ModelEvent, ModelRequest
from haven.interfaces.cli import app
from haven.ports.model import ModelPort, ProviderError
from tests.integration.harness import finish, make_repo, text, tool
from tests.integration.test_user_input import ask_turn


class AuthFailure(ScriptedModel):
    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise ProviderError("auth", "provider rejected credentials")


@pytest.fixture
def run_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Result]:
    repo = make_repo(tmp_path)
    build = bootstrap.build_services
    monkeypatch.delenv("HAVEN_BELL", raising=False)

    def invoke(model: ModelPort, *options: str, terminal: bool = True) -> Result:
        async def injected(workspace: Path, **kwargs: Any) -> bootstrap.AppServices:
            # CliRunner 在调用期间替换流；在替换后模拟实际终端或重定向。
            monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
            monkeypatch.setattr(sys.stderr, "isatty", lambda: terminal)
            return await build(workspace, model=model, **kwargs)

        monkeypatch.setattr(bootstrap, "build_services", injected)
        return CliRunner().invoke(
            app, ["run", "Inspect calc", "-w", str(repo), "--no-worktree", *options]
        )

    return invoke


@pytest.mark.parametrize("scenario", ["success", "failure", "waiting"])
def test_live_task_rings_once_for_success_failure_and_input(
    run_cli: Callable[..., Result], scenario: str
) -> None:
    if scenario == "failure":
        model = AuthFailure([])
        expected_exit = 4
    else:
        model = ScriptedModel([ask_turn() if scenario == "waiting" else [text("Done."), finish()]])
        expected_exit = 8 if scenario == "waiting" else 0
    result = run_cli(model)
    assert result.exit_code == expected_exit, result.output
    assert result.stderr == "\a"
    assert "\a" not in result.stdout
    if scenario == "failure":
        assert "Next:" in result.stdout
        assert "haven config explain" in result.stdout


@pytest.mark.parametrize("quiet", ["redirected", "disabled"])
def test_redirected_or_disabled_notifications_stay_silent(
    run_cli: Callable[..., Result], monkeypatch: pytest.MonkeyPatch, quiet: str
) -> None:
    if quiet == "disabled":
        monkeypatch.setenv("HAVEN_BELL", "0")
    result = run_cli(ScriptedModel([[text("Done."), finish()]]), terminal=quiet != "redirected")
    assert result.exit_code == 0, result.output
    assert "\a" not in result.output


def test_json_and_event_journal_do_not_gain_hints_or_control_bytes(
    run_cli: Callable[..., Result], tmp_path: Path
) -> None:
    events = tmp_path / "events.jsonl"
    result = run_cli(AuthFailure([]), "--json", "--events", str(events))
    assert result.exit_code == 4, result.output
    assert json.loads(result.stdout)["stop_reason"] == "provider_error"
    assert result.stderr == ""
    assert "Next:" not in events.read_text()
    assert all(json.loads(line) for line in events.read_text().splitlines())
    assert "\a" not in result.output


@pytest.mark.parametrize("write", [False, True])
def test_permissions_and_missing_verification_offer_distinct_next_steps(
    run_cli: Callable[..., Result], tmp_path: Path, write: bool
) -> None:
    model = ScriptedModel(
        [
            [
                tool("create", "repo.create", path="note.txt", content="hello\n"),
                finish("tool_calls"),
            ],
            [text("Done."), finish()],
        ]
    )
    options = ["--write", "--approval-policy", "all"] if write else []
    result = run_cli(model, *options)
    assert result.exit_code == (6 if write else 0), result.output
    assert (tmp_path / "repo/note.txt").exists() is write
    assert "Next:" in result.stdout
    assert ("haven discover" if write else "haven run GOAL --write") in result.stdout


def test_missing_key_explains_configuration_without_changing_exit_code(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["run", "Inspect calc", "-w", str(tmp_path)])
    assert result.exit_code == 2
    assert "no API key" in result.stdout
    assert "environment variable named above" in result.stdout
    assert "haven config explain" in result.stdout


def test_rejected_headless_approval_explains_the_default_without_granting_writes(
    run_cli: Callable[..., Result], tmp_path: Path
) -> None:
    result = run_cli(
        ScriptedModel(
            [
                [
                    tool("create", "repo.create", path="note.txt", content="hello"),
                    finish("tool_calls"),
                ],
                [text("The action was not approved."), finish()],
            ]
        ),
        "--write",
    )
    assert result.exit_code == 0, result.output
    assert "approval_rejected" in result.stdout
    assert "Next:" in result.stdout
    assert "--approval-policy" in result.stdout
    assert not (tmp_path / "repo/note.txt").exists()


def test_child_completions_and_replay_do_not_ring(
    run_cli: Callable[..., Result], tmp_path: Path
) -> None:
    events = tmp_path / "events.jsonl"
    result = run_cli(
        ScriptedModel(
            [
                [
                    tool("scout", "agent.delegate", role="scout", task="Inspect calc"),
                    finish("tool_calls"),
                ],
                [text("The function subtracts."), finish()],
                [text("Investigation complete."), finish()],
            ]
        ),
        "--events",
        str(events),
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == "\a"
    run_id = json.loads(events.read_text().splitlines()[0])["event"]["run_id"]
    replayed = CliRunner().invoke(app, ["replay", run_id])
    assert replayed.exit_code == 0, replayed.output
    assert "finished: succeeded" in replayed.stdout
    assert "\a" not in replayed.output
