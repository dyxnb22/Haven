"""TUI 表单提交、校验、取消和中断恢复。"""

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Select

from haven.application.emitter import EventEmitter
from haven.interfaces.tui.app import HavenApp
from tests.integration.harness import Harness, finish, make_repo, text
from tests.integration.test_user_input import ask_turn
from tests.tui.test_tui_journey import _Services, _settle, _submit, _wait_ready, make_builder


async def wait_for_question(app: HavenApp, pilot: Any) -> None:
    for _ in range(100):
        await pilot.pause()
        if app.screen.__class__.__name__ == "QuestionScreen":
            return
    raise AssertionError("question form never appeared")


@pytest.mark.timeout(30)
async def test_question_form_validates_before_submitting_and_resumes_the_same_run(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(
        workspace=repo,
        services_builder=make_builder(
            repo,
            [
                ask_turn(),
                [text("Inspected parser."), finish()],
            ],
        ),
    )
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect a component")
        await wait_for_question(app, pilot)
        run_id = app._state.run_id
        assert not app._state.running
        await pilot.click("#question-submit")
        assert app.screen.__class__.__name__ == "QuestionScreen"
        app.screen.query_one("#choice-0", Select).value = "Parser"
        # Textual 的按钮在点击动画期间会忽略再次点击；等待表单变更和动画完成。
        await pilot.pause(0.3)
        await pilot.click("#question-submit")
        await _settle(pilot, 25)
        assert app._state.run_id == run_id
        assert app._state.status == "succeeded"
        assert app._state.pending_input is None


@pytest.mark.timeout(30)
async def test_cancel_question_stops_the_task_without_another_model_call(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, [ask_turn()]))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect a component")
        await wait_for_question(app, pilot)
        await pilot.press("escape")
        await _settle(pilot, 20)
        assert app._state.status == "cancelled"
        model = app._services.run_service._model
        assert len(model.requests_seen) == 1


@pytest.mark.timeout(30)
async def test_restarting_the_tui_reopens_the_saved_question(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    h = Harness(repo, [ask_turn(), [text("Inspected parser."), finish()]])
    waiting = await h.service.run("Inspect a component")

    async def builder(*, workspace: Path, approvals: Any, sinks: list[Any]) -> _Services:
        # 启动新界面时，历史事件只由恢复路径重放；新事件使用新的 UI sink。
        h.service._emitter = EventEmitter(h.store, sinks)
        h.service._checkpoints._emitter = h.service._emitter
        h.service._finalizer._emitter = h.service._emitter
        return _Services(h.service, h.store)

    app = HavenApp(workspace=repo, services_builder=builder, resume_run_id=waiting.run_id)
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await wait_for_question(app, pilot)
        assert app._state.run_id == waiting.run_id
        assert len(h.model.requests_seen) == 1
        assert app._state.pending_input == waiting.pending_input
