"""终端通知只响应当前运行，历史和子代理不会再次响铃。"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from haven.interfaces.tui.app import HavenApp
from haven.ports.model import ProviderError
from tests.integration.harness import finish, make_repo, text, tool
from tests.integration.test_user_input import ask_turn
from tests.tui.test_tui_journey import _settle, _submit, _wait_ready, make_builder
from tests.tui.test_user_input import wait_for_question


@pytest.mark.parametrize("scenario", ["success", "failure", "waiting"])
async def test_live_tui_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    monkeypatch.delenv("HAVEN_BELL", raising=False)
    repo = make_repo(tmp_path)
    turns = [ask_turn()] if scenario == "waiting" else [[text("Done."), finish()]]
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, turns))
    bell = Mock()
    monkeypatch.setattr(app, "bell", bell)
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        if scenario == "failure":
            monkeypatch.setattr(
                app._svc.run_service._model,
                "generate_stream",
                Mock(side_effect=ProviderError("auth", "provider rejected credentials")),
            )
        await _submit(app, pilot, "Inspect calc")
        if scenario == "waiting":
            await wait_for_question(app, pilot)
        else:
            await _settle(pilot, 15)
            assert app._state.status == ("failed" if scenario == "failure" else "succeeded")
        bell.assert_called_once_with()
        if scenario == "failure":
            assert any("haven config explain" in entry.text for entry in app._state.timeline)


async def test_tui_notification_can_be_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HAVEN_BELL", "0")
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, [[text("Done."), finish()]]))
    bell = Mock()
    monkeypatch.setattr(app, "bell", bell)
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect calc")
        await _settle(pilot, 15)
        assert app._state.status == "succeeded"
        bell.assert_not_called()


async def test_tui_child_completion_and_loading_history_are_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HAVEN_BELL", raising=False)
    repo = make_repo(tmp_path)
    app = HavenApp(
        workspace=repo,
        services_builder=make_builder(
            repo,
            [
                [
                    tool("scout", "agent.delegate", role="scout", task="Inspect calc"),
                    finish("tool_calls"),
                ],
                [text("The function subtracts."), finish()],
                [text("Investigation complete."), finish()],
            ],
        ),
    )
    bell = Mock()
    monkeypatch.setattr(app, "bell", bell)
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect calc")
        await _settle(pilot, 20)
        assert app._state.status == "succeeded"
        bell.assert_called_once_with()
        assert await app._load_session(app._state.run_id)
        bell.assert_called_once_with()
