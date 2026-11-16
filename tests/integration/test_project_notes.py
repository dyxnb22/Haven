"""显式笔记进入新上下文，重启、隔离、恢复和读取失败的边界。"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from haven.adapters.providers.scripted import ScriptedModel
from haven.application.approvals import AutoApprover
from haven.bootstrap import build_context_preview, build_services
from haven.config import db_path
from haven.contracts.user_input import UserInputResponse
from haven.domain.enums import PermissionMode, RunStatus
from tests.integration.harness import finish, make_repo, text
from tests.integration.test_guidance_reload import build
from tests.integration.test_user_input import ask_turn


async def test_notes_use_their_own_untrusted_context_and_appear_in_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("Repository guidance")
    model = ScriptedModel([[text("Done."), finish()]], repeat_last=True)
    services = await build(repo, db_path(), model)
    try:
        service = services.run_service
        original = service.guidance_snapshot
        monkeypatch.setattr(
            services.workspace,
            "read_file",
            AsyncMock(side_effect=AssertionError("note edits must not re-read guidance")),
        )
        await service.update_project_notes("remember", "Use uv run for project commands")
        assert service.guidance_snapshot.text == original.text
        assert model.requests_seen == []
        first = await service.run("Inspect the project")
        note_messages = [
            message
            for message in model.requests_seen[0].messages
            if 'source="user_project_notes"' in message.content
        ]
        assert len(note_messages) == 1 and note_messages[0].role == "user"
        assert "UNTRUSTED DATA" in note_messages[0].content
        assert "Use uv run" in note_messages[0].content
        request, segments, _ = await build_context_preview(repo, "Preview the project")
        assert any("Use uv run" in message.content for message in request.messages)
        assert any(s.source == "project_notes" and s.trust == "untrusted" for s in segments)
        saved = await services.store.load_checkpoint(first.run_id)
        assert saved is not None and saved.guidance_snapshot == service.guidance_snapshot
        await service.update_project_notes("clear")
        await service.continue_run(first.run_id, "Inspect another detail")
        assert all(
            "Use uv run" not in message.content for message in model.requests_seen[1].messages
        )
        assert await services.store.load_checkpoint(first.run_id) == saved
    finally:
        await services.close()


async def test_restart_answer_keeps_notes_deleted_from_the_current_project(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    database = tmp_path / "sessions.db"
    first = await build(repo, database, ScriptedModel([ask_turn()]))
    try:
        await first.run_service.update_project_notes("remember", "Original saved project note")
        frozen = first.run_service.guidance_snapshot
        waiting = await first.run_service.run("Inspect a component")
        assert waiting.pending_input is not None
        with pytest.raises(ValueError, match="pending question"):
            await first.run_service.update_project_notes("clear")
    finally:
        await first.close()

    model = ScriptedModel([[text("Done."), finish()]])
    restarted = await build(repo, database, model)
    try:
        assert restarted.run_service.guidance_snapshot.notes == frozen.notes
        await restarted.run_service.update_project_notes("clear")
        await restarted.run_service.update_project_notes("remember", "New saved project note")
        result = await restarted.run_service.answer_run(
            waiting.run_id,
            UserInputResponse(
                request_id=waiting.pending_input.request_id, answers={"scope": "Parser"}
            ),
        )
        assert result.status is RunStatus.SUCCEEDED
        assert any(
            "Original saved project note" in m.content for m in model.requests_seen[0].messages
        )
        assert all(
            "New saved project note" not in m.content for m in model.requests_seen[0].messages
        )
        saved = await restarted.store.load_checkpoint(waiting.run_id)
        assert saved is not None and saved.guidance_snapshot == frozen
        assert [n.text for n in await restarted.store.list_project_notes(str(repo.resolve()))] == [
            "New saved project note"
        ]
    finally:
        await restarted.close()


async def test_stored_notes_do_not_cross_projects_or_isolated_worktrees(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    for argv in [
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "init",
        ],
    ]:
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    database = tmp_path / "sessions.db"

    async def isolated():  # type: ignore[no-untyped-def]
        return await build_services(
            repo,
            approvals=AutoApprover("approve_all"),
            sinks=[],
            model=ScriptedModel([]),
            store_path=database,
            mode=PermissionMode.INTERACTIVE,
            worktree=True,
        )

    first = await isolated()
    try:
        first_root = first.workspace.root
        assert first_root != repo
        await first.run_service.update_project_notes("remember", "Note for the main project")
        assert len(await first.store.list_project_notes(str(repo.resolve()))) == 1
        assert await first.store.list_project_notes(str(first_root)) == ()
    finally:
        await first.close()
    second = await isolated()
    try:
        assert second.workspace.root != first_root
        assert [note.text for note in second.run_service.guidance_snapshot.notes] == [
            "Note for the main project"
        ]
    finally:
        await second.close()
    other = await build(make_repo(tmp_path / "other"), database, ScriptedModel([]))
    try:
        assert other.run_service.guidance_snapshot.notes == ()
    finally:
        await other.close()


async def test_storage_failures_leave_the_default_and_reload_reconciles_saved_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    services = await build(repo, tmp_path / "sessions.db", ScriptedModel([]))
    try:
        service = services.run_service
        original = service.guidance_snapshot
        with monkeypatch.context() as patch:
            patch.setattr(
                services.store, "add_project_note", AsyncMock(side_effect=OSError("write"))
            )
            with pytest.raises(OSError, match="write"):
                await service.update_project_notes("remember", "not saved")
        assert service.guidance_snapshot is original and not service.is_reloading_guidance
        with monkeypatch.context() as patch:
            patch.setattr(
                services.store, "list_project_notes", AsyncMock(side_effect=OSError("read"))
            )
            with pytest.raises(OSError, match="read"):
                await service.update_project_notes("remember", "saved before the failed read")
        assert service.guidance_snapshot is original and not service.is_reloading_guidance
        refreshed = await service.reload_guidance()
        assert [note.text for note in refreshed.notes] == ["saved before the failed read"]
    finally:
        await services.close()
