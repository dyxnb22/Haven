"""会话选择、全新对话和打开历史不重放副作用。"""

from pathlib import Path

from textual.widgets import Input, Select

from haven.interfaces.tui.app import HavenApp
from haven.interfaces.tui.sessions import SessionPickerScreen
from tests.integration.harness import finish, make_repo, text
from tests.tui.test_tui_journey import _settle, _submit, _wait_ready, make_builder


async def test_new_search_open_and_continue_history(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(
        workspace=repo,
        services_builder=make_builder(
            repo,
            [
                [text("first answer"), finish()],
                [text("fresh answer"), finish()],
                [text("continued first"), finish()],
            ],
        ),
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "first goal alpha")
        await _settle(pilot, 20)
        first = app._state.run_id
        await _submit(app, pilot, "/new")
        assert app._state.run_id == ""
        await _submit(app, pilot, "second goal beta")
        await _settle(pilot, 20)
        second = app._state.run_id
        assert second != first
        checkpoint = await app._svc.store.load_checkpoint(second)
        assert checkpoint is not None
        assert all("first answer" not in m.content for m in checkpoint.messages)
        await _submit(app, pilot, "/sessions alpha")
        await _settle(pilot, 10)
        assert isinstance(app.screen, SessionPickerScreen)
        assert app.screen.query_one("#session-choice", Select).value == first
        app.screen.query_one("#session-search", Input).value = "beta"
        await _settle(pilot, 5)
        assert app.screen.query_one("#session-choice", Select).value == second
        app.screen.query_one("#session-search", Input).value = "alpha"
        await _settle(pilot, 5)
        await pilot.click("#session-open")
        await _settle(pilot, 10)
        assert app._state.run_id == first
        assert not app._state.running
        assert "first answer" in app._state.chat_text
        await _submit(app, pilot, "continue alpha please")
        await _settle(pilot, 20)
        checkpoint = await app._svc.store.load_checkpoint(app._state.run_id)
        assert checkpoint is not None
        assert any("first answer" in m.content for m in checkpoint.messages)
        assert all("fresh answer" not in m.content for m in checkpoint.messages)
