"""通过实际按键和 SQLite 重启验证任务历史、草稿与焦点隔离。"""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Input

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.application.approvals import ApprovalResponder
from haven.contracts.model import ModelEvent
from haven.domain.enums import PermissionMode
from haven.interfaces.tui.app import HavenApp, ServicesBuilder
from haven.ports.event_sink import EventSinkPort
from tests.integration.harness import finish, make_repo, text
from tests.tui.test_tui_journey import _settle, _submit, _wait_ready, make_builder


def persisted_builder(database: Path) -> ServicesBuilder:
    async def build(
        *, workspace: Path, approvals: ApprovalResponder, sinks: list[EventSinkPort]
    ) -> bootstrap.AppServices:
        turns: list[list[ModelEvent]] = [
            [text("First answer."), finish()],
            [text("Follow-up answer."), finish()],
        ]
        return await bootstrap.build_services(
            workspace,
            approvals=approvals,
            sinks=sinks,
            model=ScriptedModel(turns),
            mode=PermissionMode.INTERACTIVE,
            store_path=database,
            worktree=False,
        )

    return build


async def test_history_restores_the_draft_and_survives_restart(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    build = persisted_builder(tmp_path / "sessions.db")
    first, second, draft = "Inspect @src/calc.py", "Explain the subtraction", "第三个任务的草稿"
    app = HavenApp(workspace=repo, services_builder=build)
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, first)
        await _settle(pilot, 15)
        await _submit(app, pilot, second)
        await _settle(pilot, 15)
        assert app._state.status == "succeeded"
        prompt = app.query_one("#prompt", Input)
        prompt.focus()
        prompt.value = draft
        await pilot.press("up")
        assert prompt.value == second
        await pilot.press("up", "up")
        assert prompt.value == first
        await pilot.press("down")
        assert prompt.value == second
        await pilot.press("down", "down")
        assert prompt.value == draft
        await pilot.press("up")
        prompt.value += " with more details"
        await pilot.press("down")
        assert prompt.value == draft
        # 原始 @提及与后续请求均保存，草稿和编辑中的历史条目没有写回。
        assert await app._svc.store.load_input_history(str(repo.resolve())) == [first, second]

    restarted = HavenApp(workspace=repo, services_builder=build)
    async with restarted.run_test() as pilot:
        await _wait_ready(restarted, pilot)
        prompt = restarted.query_one("#prompt", Input)
        assert prompt.value == ""
        prompt.focus()
        await pilot.press("up")
        assert prompt.value == second
        await pilot.press("up")
        assert prompt.value == first
        await pilot.press("down", "down")
        assert prompt.value == ""
        assert restarted._svc.run_service._model.requests_seen == []
        await pilot.press("up")
        revised = second + " and cover edge cases"
        prompt.value = revised
        await pilot.press("enter")
        await _settle(pilot, 15)
        assert restarted._state.status == "succeeded"
        assert await restarted._svc.store.load_input_history(str(repo.resolve())) == [
            first,
            second,
            revised,
        ]
        await pilot.press("up")
        assert prompt.value == revised

    other = make_repo(tmp_path / "other")
    isolated = HavenApp(workspace=other, services_builder=build)
    async with isolated.run_test() as pilot:
        await _wait_ready(isolated, pilot)
        prompt = isolated.query_one("#prompt", Input)
        prompt.focus()
        prompt.value = "A different workspace's draft"
        await pilot.press("up", "down")
        assert prompt.value == "A different workspace's draft"


async def test_history_commands_and_session_search_keep_their_own_keys(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, [[text("Done."), finish()]]))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect the arithmetic")
        await _settle(pilot, 15)
        await _submit(app, pilot, "/sessions")
        await _settle(pilot, 10)
        search = app.screen.query_one("#session-search", Input)
        search.focus()
        search.value = "search draft"
        await pilot.press("up", "down")
        assert search.value == "search draft"
        assert app.query_one("#prompt", Input).value == ""
        await pilot.press("escape")
        await _submit(app, pilot, "/help")
        prompt = app.query_one("#prompt", Input)
        prompt.focus()
        await pilot.press("up")
        assert prompt.value == "Inspect the arithmetic"
        assert await app._svc.store.load_input_history(str(repo.resolve())) == [
            "Inspect the arithmetic"
        ]


async def test_history_read_and_write_failures_do_not_block_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    build = make_builder(repo, [[text("Done."), finish()]])

    async def failing_history(**kwargs: Any) -> Any:
        services = await build(**kwargs)
        monkeypatch.setattr(
            services.store, "load_input_history", AsyncMock(side_effect=OSError("read failed"))
        )
        monkeypatch.setattr(
            services.store, "record_input", AsyncMock(side_effect=OSError("write failed"))
        )
        return services

    app = HavenApp(workspace=repo, services_builder=failing_history)
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect the arithmetic")
        await _settle(pilot, 15)
        assert app._state.status == "succeeded"
        lines = "\n".join(entry.text for entry in app._state.timeline)
        assert "input history could not be loaded" in lines
        assert "input history could not be saved" in lines
        prompt = app.query_one("#prompt", Input)
        prompt.focus()
        await pilot.press("up")
        assert prompt.value == "Inspect the arithmetic"


async def test_history_management_preserves_draft_runs_and_other_workspaces(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    build = persisted_builder(tmp_path / "sessions.db")
    app = HavenApp(workspace=repo, services_builder=build)
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "First task to inspect")
        await _settle(pilot, 15)
        await _submit(app, pilot, "Second task to inspect")
        await _settle(pilot, 15)
        run_id = app._state.run_id
        checkpoint = await app._svc.store.load_checkpoint(run_id)
        await app._svc.store.record_input("/other", "keep this task")
        prompt = app.query_one("#prompt", Input)
        prompt.value = "Keep this unfinished draft"
        prompt.focus()
        await pilot.press("up")
        await _submit(app, pilot, "/history")
        await _settle(pilot, 5)
        assert prompt.value == "Keep this unfinished draft"
        history = app._state.timeline[-1].text
        assert "1. Second task to inspect" in history
        assert "2. First task to inspect" in history
        await pilot.press("up")
        await _submit(app, pilot, "/history clear")
        await _settle(pilot, 5)
        assert prompt.value == "Keep this unfinished draft"
        await pilot.press("up", "down")
        assert prompt.value == "Keep this unfinished draft"
        assert await app._svc.store.load_input_history(str(repo.resolve())) == []
        assert await app._svc.store.load_input_history("/other") == ["keep this task"]
        assert await app._svc.store.load_checkpoint(run_id) == checkpoint
        assert await app._svc.store.get_run(run_id) is not None
        assert len(app._svc.run_service._model.requests_seen) == 2
    restarted = HavenApp(workspace=repo, services_builder=build)
    async with restarted.run_test() as pilot:
        await _wait_ready(restarted, pilot)
        prompt = restarted.query_one("#prompt", Input)
        prompt.focus()
        await pilot.press("up")
        assert prompt.value == ""


async def test_failed_or_invalid_history_clear_keeps_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=make_builder(repo, [[text("Done."), finish()]]))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect this task")
        await _settle(pilot, 15)
        clear = AsyncMock(side_effect=OSError("database is read-only"))
        monkeypatch.setattr(app._svc.store, "clear_input_history", clear)
        await _submit(app, pilot, "/history all")
        assert "usage: /history [clear]" in app._state.timeline[-1].text
        clear.assert_not_called()
        prompt = app.query_one("#prompt", Input)
        prompt.value = "Keep the draft"
        prompt.focus()
        await pilot.press("up")
        await _submit(app, pilot, "/history clear")
        await _settle(pilot, 5)
        assert "could not be cleared" in app._state.timeline[-1].text
        assert prompt.value == "Keep the draft"
        await pilot.press("up")
        assert prompt.value == "Inspect this task"
        assert await app._svc.store.load_input_history(str(repo.resolve())) == ["Inspect this task"]
        assert len(app._svc.run_service._model.requests_seen) == 1


async def test_loading_history_preserves_a_draft_typed_during_startup(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    release = asyncio.Event()
    build = make_builder(repo, [])

    async def delayed_history(**kwargs: Any) -> Any:
        services = await build(**kwargs)
        await services.store.record_input(str(repo.resolve()), "An earlier task")
        await release.wait()
        return services

    app = HavenApp(workspace=repo, services_builder=delayed_history)
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.focus()
        prompt.value = "A draft before startup finishes"
        release.set()
        await _wait_ready(app, pilot)
        assert prompt.value == "A draft before startup finishes"
        await pilot.press("up")
        assert prompt.value == "An earlier task"
        await pilot.press("down")
        assert prompt.value == "A draft before startup finishes"
