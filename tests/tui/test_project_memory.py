"""项目指引查看与空闲刷新；查看不会读变更后的文件或启动模型。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Static

from haven.interfaces.tui.app import HavenApp
from haven.interfaces.tui.memory import ProjectMemoryScreen
from haven.interfaces.tui.prompt import PromptInput
from tests.integration.harness import make_repo
from tests.tui.test_input_history import persisted_builder
from tests.tui.test_tui_journey import _settle, _submit, _wait_ready


async def test_memory_shows_the_loaded_snapshot_and_keeps_model_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("Original root instructions [red]literal[/red]\x1b[31m")
    (repo / "src/AGENTS.md").write_text("Original source instructions")
    app = HavenApp(workspace=repo, services_builder=persisted_builder(tmp_path / "sessions.db"))
    async with app.run_test(size=(80, 24)) as pilot:
        await _wait_ready(app, pilot)
        snapshot = app._svc.run_service.guidance_snapshot
        assert "Original root instructions" in snapshot.text
        (repo / "AGENTS.md").write_text("Changed after startup")
        reads = AsyncMock(side_effect=AssertionError("memory view must not re-read files"))
        monkeypatch.setattr(app._svc.run_service._workspace, "read_file", reads)
        await _submit(app, pilot, "/memory")
        assert isinstance(app.screen, ProjectMemoryScreen)
        assert app.screen._snapshot == snapshot
        assert "Changed after startup" not in app.screen._snapshot.text
        source_text = str(app.screen.query_one("#memory-sources", Static).render())
        assert "src/AGENTS.md" in source_text
        assert "scope: src" in source_text
        assert "CLAUDE.md" in source_text and "missing" in source_text
        assert "\x1b" not in str(app.screen.query_one("#memory-text", Static).render())
        assert app._svc.run_service._model.requests_seen == []
        reads.assert_not_called()
        await pilot.press("escape")
        await _submit(app, pilot, "Inspect the loaded instructions")
        await _settle(pilot, 15)
        assert app._state.status == "succeeded"
        request = app._svc.run_service._model.requests_seen[0]
        assert any("Original root instructions" in message.content for message in request.messages)
        assert all("Changed after startup" not in message.content for message in request.messages)
        reads.assert_not_called()


async def test_memory_handles_an_empty_snapshot_and_closes_without_a_task(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=persisted_builder(tmp_path / "sessions.db"))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "/memory")
        assert isinstance(app.screen, ProjectMemoryScreen)
        assert app.screen._snapshot.text == ""
        assert all(source.status == "missing" for source in app.screen._snapshot.sources)
        assert "no project guidance" in str(app.screen.query_one("#memory-text", Static).render())
        await pilot.click("#memory-close")
        assert not isinstance(app.screen, ProjectMemoryScreen)
        assert app._svc.run_service._model.requests_seen == []


async def test_memory_reload_is_explicit_and_the_next_task_uses_it(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    guidance_file = repo / "AGENTS.md"
    guidance_file.write_text("Original instructions")
    app = HavenApp(workspace=repo, services_builder=persisted_builder(tmp_path / "sessions.db"))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        original = app._svc.run_service.guidance_snapshot
        guidance_file.write_text("Updated instructions")
        await _submit(app, pilot, "/memory reload extra")
        assert app._svc.run_service.guidance_snapshot is original
        assert "usage: /memory [reload]" in app._state.timeline[-1].text
        await _submit(app, pilot, "/memory reload")
        await _settle(pilot, 5)
        updated = app._svc.run_service.guidance_snapshot
        assert "Updated instructions" in updated.text
        assert app._svc.run_service._model.requests_seen == []
        assert await app._svc.store.load_input_history(str(repo.resolve())) == []
        await _submit(app, pilot, "/memory")
        assert isinstance(app.screen, ProjectMemoryScreen)
        assert app.screen._snapshot == updated
        await pilot.press("escape")
        await _submit(app, pilot, "Inspect with the updated instructions")
        await _settle(pilot, 15)
        request = app._svc.run_service._model.requests_seen[0]
        assert any("Updated instructions" in message.content for message in request.messages)
        assert all("Original instructions" not in message.content for message in request.messages)


async def test_reload_failure_keeps_the_snapshot_and_typing_during_reload_keeps_the_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=persisted_builder(tmp_path / "sessions.db"))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        service = app._svc.run_service
        original = service.guidance_snapshot
        loader = service._guidance_loader
        monkeypatch.setattr(
            service, "_guidance_loader", AsyncMock(side_effect=OSError("unreadable"))
        )
        await _submit(app, pilot, "/memory reload")
        await _settle(pilot, 3)
        assert service.guidance_snapshot is original
        assert "could not be reloaded" in app._state.timeline[-1].text
        assert not service.is_reloading_guidance
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed():  # type: ignore[no-untyped-def]
            entered.set()
            await release.wait()
            assert loader is not None
            return await loader()

        monkeypatch.setattr(service, "_guidance_loader", delayed)
        await _submit(app, pilot, "/memory reload")
        try:
            await asyncio.wait_for(entered.wait(), 2)
            draft = "Keep this task draft while loading"
            await _submit(app, pilot, draft)
            assert app.query_one("#prompt", PromptInput).value == draft
            assert service._model.requests_seen == []
            assert await app._svc.store.load_input_history(str(repo.resolve())) == []
        finally:
            release.set()
            await _settle(pilot, 3)
        assert not service.is_reloading_guidance
        assert app.query_one("#prompt", PromptInput).value == draft


async def test_remember_view_delete_and_clear_survive_restarts_without_model_calls(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    builder = persisted_builder(tmp_path / "sessions.db")
    app = HavenApp(workspace=repo, services_builder=builder)
    async with app.run_test(size=(80, 24)) as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "/remember 这个项目用 uv run 执行命令")
        await _settle(pilot, 4)
        notes = app._svc.run_service.guidance_snapshot.notes
        assert len(notes) == 1 and "uv run" in notes[0].text
        assert app._svc.run_service._model.requests_seen == []
        assert await app._svc.store.load_input_history(str(repo.resolve())) == []
        await _submit(app, pilot, "/memory")
        assert isinstance(app.screen, ProjectMemoryScreen)
        rendered = str(app.screen.query_one("#memory-notes", Static).render())
        assert str(notes[0].id) in rendered and notes[0].created_at in rendered
        assert "uv run" in rendered
        await pilot.press("escape")
        await _submit(app, pilot, "Inspect the project")
        await _settle(pilot, 15)
        run_id = app._state.run_id
        assert any(
            "这个项目用 uv run" in message.content
            for message in app._svc.run_service._model.requests_seen[0].messages
        )
    restarted = HavenApp(workspace=repo, services_builder=builder)
    async with restarted.run_test() as pilot:
        await _wait_ready(restarted, pilot)
        assert restarted._svc.run_service.guidance_snapshot.notes == notes
        await _submit(restarted, pilot, f"/memory forget {notes[0].id}")
        await _settle(pilot, 4)
        assert restarted._svc.run_service.guidance_snapshot.notes == ()
        await _submit(restarted, pilot, "/remember a second note")
        await _settle(pilot, 4)
        other = await restarted._svc.store.add_project_note("/another", "keep the other project")
        await _submit(restarted, pilot, "/memory clear")
        await _settle(pilot, 4)
        assert restarted._svc.run_service.guidance_snapshot.notes == ()
        assert await restarted._svc.store.list_project_notes("/another") == (other,)
        checkpoint = await restarted._svc.store.load_checkpoint(run_id)
        assert checkpoint is not None and checkpoint.guidance_snapshot is not None
        assert checkpoint.guidance_snapshot.notes == notes
        assert await restarted._svc.store.load_input_history(str(repo.resolve())) == [
            "Inspect the project"
        ]
        assert restarted._svc.run_service._model.requests_seen == []
    again = HavenApp(workspace=repo, services_builder=builder)
    async with again.run_test() as pilot:
        await _wait_ready(again, pilot)
        assert again._svc.run_service.guidance_snapshot.notes == ()


async def test_invalid_note_operations_preserve_both_projects_and_report_storage_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    app = HavenApp(workspace=repo, services_builder=persisted_builder(tmp_path / "sessions.db"))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "/remember Keep this note")
        await _settle(pilot, 4)
        original = app._svc.run_service.guidance_snapshot
        other = await app._svc.store.add_project_note("/other", "Do not delete this")
        for command in [
            "/remember",
            "/remember " + "x" * 501,
            "/memory forget 0",
            "/memory forget nope",
            f"/memory forget {other.id}",
            "/memory clear extra",
        ]:
            await _submit(app, pilot, command)
            await _settle(pilot, 2)
            assert app._svc.run_service.guidance_snapshot == original
        assert await app._svc.store.list_project_notes("/other") == (other,)
        monkeypatch.setattr(
            app._svc.store, "add_project_note", AsyncMock(side_effect=OSError("write failed"))
        )
        await _submit(app, pilot, "/remember Cannot save this")
        await _settle(pilot, 3)
        assert "reconcile saved notes" in app._state.timeline[-1].text
        assert app._svc.run_service.guidance_snapshot == original
        assert app._svc.run_service._model.requests_seen == []
