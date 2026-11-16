"""暂停中可维护交接；校验失败与并发冲突保留当前表单。"""

from pathlib import Path

from textual.widgets import Static, TextArea

from haven.adapters.providers.scripted import ScriptedModel
from haven.application.handoff import view_handoff
from haven.contracts.events import Notice
from haven.interfaces.tui.app import HavenApp
from haven.interfaces.tui.handoff import HandoffScreen
from tests.integration.harness import make_repo
from tests.integration.test_user_input import ask_turn
from tests.tui.test_tui_journey import _settle, _submit, _wait_ready
from tests.tui.test_verification_groups import builder


async def test_edit_pending_task_handoff_without_consuming_answer_or_calling_a_model(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    model = ScriptedModel([ask_turn()])
    app = HavenApp(workspace=repo, services_builder=builder(tmp_path / "sessions.db", model))
    async with app.run_test(size=(110, 45)) as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect safely")
        await _settle(pilot, 12)
        await pilot.click("#question-keep")
        assert app._state.pending_input is not None
        pending = app._state.pending_input
        await _submit(app, pilot, "/handoff edit")
        await _settle(pilot, 3)
        assert isinstance(app.screen, HandoffScreen)
        app.screen.query_one("#handoff-decisions", TextArea).load_text("x" * 601)
        await pilot.click("#handoff-save")
        assert isinstance(app.screen, HandoffScreen)
        assert "600" in str(app.screen.query_one("#handoff-error", Static).content)
        app.screen.query_one("#handoff-decisions", TextArea).load_text(
            "Keep the API because callers rely on it"
        )
        await app._svc.store.append_event(
            app._state.run_id,
            Notice(run_id=app._state.run_id, level="info", message="concurrent journal update"),
        )
        await pilot.pause(0.35)  # 等待按钮的点击动画结束，第二次点击才会触发保存。
        await pilot.click("#handoff-save")
        assert isinstance(app.screen, HandoffScreen)
        assert "reopen" in str(app.screen.query_one("#handoff-error", Static).content)
        assert "callers rely" in app.screen.query_one("#handoff-decisions", TextArea).text
        await pilot.press("escape")
        await _submit(app, pilot, "/handoff edit")
        await _settle(pilot, 3)
        app.screen.query_one("#handoff-decisions", TextArea).load_text(
            "Keep the API because callers rely on it"
        )
        await pilot.click("#handoff-save")
        await _settle(pilot, 3)
        assert not isinstance(app.screen, HandoffScreen)
        view = await view_handoff(app._svc.store, app._state.run_id)
        assert view.snapshot is not None and "callers rely" in view.snapshot.notes.decisions
        assert view.checkpoint.pending_input == pending and app._state.pending_input == pending
        assert len(model.requests_seen) == 1
        await _submit(app, pilot, "/handoff history")
        await _settle(pilot, 3)
        assert isinstance(app.screen, HandoffScreen)
        assert "callers rely" in str(app.screen.query_one("#handoff-view", Static).content)
        await pilot.press("escape")
