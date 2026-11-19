"""项目指引刷新只改变新运行；恢复保持检查点中的资料。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from haven.adapters.providers.scripted import ScriptedModel
from haven.application.approvals import AutoApprover
from haven.application.run_service import build_run_context_from_checkpoint
from haven.bootstrap import AppServices, build_services
from haven.contracts.guidance import GuidanceSnapshot
from haven.contracts.model import ModelEvent, ModelRequest
from haven.contracts.user_input import UserInputResponse
from haven.domain.enums import PermissionMode, RunStatus
from tests.integration.harness import CollectingSink, finish, make_repo, text
from tests.integration.test_user_input import ask_turn


async def build(
    repo: Path, database: Path, model: ScriptedModel, sink: CollectingSink | None = None
) -> AppServices:
    return await build_services(
        repo,
        approvals=AutoApprover("approve_all"),
        sinks=[sink] if sink else [],
        model=model,
        store_path=database,
        mode=PermissionMode.INTERACTIVE,
        worktree=False,
    )


async def test_reload_changes_new_runs_and_follow_ups_without_rewriting_old_runs(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    guidance_file = repo / "AGENTS.md"
    guidance_file.write_text("Original project instructions")
    model = ScriptedModel([[text("Done."), finish()]], repeat_last=True)
    services = await build(repo, tmp_path / "sessions.db", model)
    try:
        service = services.run_service
        original = service.guidance_snapshot
        first = await service.run("Inspect the project")
        guidance_file.write_text("Updated project instructions")
        skills, recipes, approvals = service._skills, service._recipes, service.approval_mode
        updated = await service.reload_guidance()
        assert updated != original
        assert "Updated project instructions" in updated.text
        assert len(model.requests_seen) == 1
        assert service._skills is skills and service._recipes is recipes
        assert service.approval_mode is approvals

        second = await service.continue_run(first.run_id, "Explain another detail")
        third = await service.run("Inspect another part")
        for outcome in (second, third):
            checkpoint = await services.store.load_checkpoint(outcome.run_id)
            assert checkpoint is not None and checkpoint.guidance_snapshot == updated
        old_checkpoint = await services.store.load_checkpoint(first.run_id)
        assert old_checkpoint is not None and old_checkpoint.guidance_snapshot == original
        assert any(
            "Original project instructions" in m.content for m in model.requests_seen[0].messages
        )
        for request in model.requests_seen[1:]:
            assert any("Updated project instructions" in m.content for m in request.messages)
            assert all("Original project instructions" not in m.content for m in request.messages)
    finally:
        await services.close()


@pytest.mark.parametrize("initial_guidance", ["", "Original project instructions"])
async def test_answer_after_restart_uses_saved_guidance_including_an_empty_snapshot(
    tmp_path: Path, initial_guidance: str
) -> None:
    repo = make_repo(tmp_path)
    if initial_guidance:
        (repo / "AGENTS.md").write_text(initial_guidance)
    database = tmp_path / "sessions.db"
    first = await build(repo, database, ScriptedModel([ask_turn()]))
    try:
        original = first.run_service.guidance_snapshot
        assert bool(original.text) == bool(initial_guidance)
        waiting = await first.run_service.run("Inspect a component")
        assert waiting.pending_input is not None
        with pytest.raises(ValueError, match="pending question"):
            await first.run_service.reload_guidance()
    finally:
        await first.close()

    (repo / "AGENTS.md").write_text("Only new tasks should see these instructions")
    model = ScriptedModel([[text("Inspected parser."), finish()]])
    reopened = await build(repo, database, model)
    try:
        assert reopened.run_service.guidance_snapshot.text != ""
        outcome = await reopened.run_service.answer_run(
            waiting.run_id,
            UserInputResponse(
                request_id=waiting.pending_input.request_id, answers={"scope": "Parser"}
            ),
        )
        assert outcome.status is RunStatus.SUCCEEDED
        assert all(
            "Only new tasks" not in message.content for message in model.requests_seen[0].messages
        )
        if initial_guidance:
            assert any(
                initial_guidance in message.content for message in model.requests_seen[0].messages
            )
        checkpoint = await reopened.store.load_checkpoint(waiting.run_id)
        assert checkpoint is not None and checkpoint.guidance_snapshot == original
    finally:
        await reopened.close()


async def test_legacy_resume_warns_and_pins_the_current_snapshot(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    model = ScriptedModel([ask_turn(), [text("Done."), finish()]])
    sink = CollectingSink()
    services = await build(repo, tmp_path / "sessions.db", model, sink)
    try:
        waiting = await services.run_service.run("Inspect a component")
        assert waiting.pending_input is not None
        checkpoint = await services.store.load_checkpoint(waiting.run_id)
        assert checkpoint is not None
        await services.store.save_checkpoint(
            checkpoint.model_copy(update={"guidance_snapshot": None})
        )
        current = services.run_service.guidance_snapshot
        await services.run_service.answer_run(
            waiting.run_id,
            UserInputResponse(
                request_id=waiting.pending_input.request_id, answers={"scope": "CLI"}
            ),
        )
        notices = sink.events_of("notice")
        assert any("legacy checkpoint" in getattr(event, "message", "") for event in notices)
        saved = await services.store.load_checkpoint(waiting.run_id)
        assert saved is not None and saved.guidance_snapshot == current
    finally:
        await services.close()


async def test_reload_failure_and_cancellation_keep_the_default_and_block_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    model = ScriptedModel([[text("Done."), finish()]], repeat_last=True)
    services = await build(repo, tmp_path / "sessions.db", model)
    service = services.run_service
    try:
        first = await service.run("Inspect the project")
        checkpoint = await services.store.load_checkpoint(first.run_id)
        assert checkpoint is not None
        original = service.guidance_snapshot
        loader = service._guidance_loader
        monkeypatch.setattr(
            service, "_guidance_loader", AsyncMock(side_effect=OSError("unreadable"))
        )
        with pytest.raises(OSError, match="unreadable"):
            await service.reload_guidance()
        assert service.guidance_snapshot is original
        assert not service.is_reloading_guidance

        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed() -> GuidanceSnapshot:
            entered.set()
            await release.wait()
            return GuidanceSnapshot(text="must not be published after cancellation")

        monkeypatch.setattr(service, "_guidance_loader", delayed)
        task = asyncio.create_task(service.reload_guidance())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert service.is_reloading_guidance
            with pytest.raises(ValueError, match="reloading"):
                await service.run("Do not start yet")
            with pytest.raises(ValueError, match="reloading"):
                await service.continue_run(first.run_id, "Do not continue yet")
            with pytest.raises(ValueError, match="reloading"):
                await service.resume(build_run_context_from_checkpoint(checkpoint))
            with pytest.raises(ValueError, match="reloading"):
                await service.reload_guidance()
            with pytest.raises(ValueError, match="reloading"):
                await service.update_project_notes("remember", "Do not save yet")
            assert len(model.requests_seen) == 1
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not service.is_reloading_guidance
        assert service.guidance_snapshot is original
        monkeypatch.setattr(service, "_guidance_loader", loader)
        with pytest.raises(ValueError, match="goal must be"):
            await service.run("x")
        assert await service.reload_guidance() == original
    finally:
        await services.close()


@pytest.mark.parametrize("stage", ["preparing", "executing"])
async def test_active_runs_and_pending_reviews_block_reload_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            if stage == "executing":
                entered.set()
                await release.wait()
            async for event in super().generate_stream(request):
                yield event

    model = WaitingModel([[text("Done."), finish()]])
    services = await build(make_repo(tmp_path), tmp_path / "sessions.db", model)
    service = services.run_service
    if stage == "preparing":

        async def initializing(run_id: str) -> None:
            entered.set()
            await release.wait()

        monkeypatch.setattr(service, "_initialize_mcp", initializing)
    reads = AsyncMock(return_value=GuidanceSnapshot(text="must not load while busy"))
    assert service._project_memory is not None
    writes = AsyncMock()
    monkeypatch.setattr(service._project_memory, "update", writes)
    monkeypatch.setattr(service, "_guidance_loader", reads)
    task = asyncio.create_task(service.run("Inspect the project"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ValueError, match="run is active"):
            await service.reload_guidance()
        reads.assert_not_called()
        with pytest.raises(ValueError, match="run is active"):
            await service.update_project_notes("remember", "Do not save while busy")
        writes.assert_not_called()
        release.set()
        outcome = await task
        service._pending_reviews.add(outcome.run_id)
        with pytest.raises(ValueError, match="pending question or review"):
            await service.reload_guidance()
        reads.assert_not_called()
        service.mark_reviewed(outcome.run_id)
        assert await service.reload_guidance() == reads.return_value
    finally:
        release.set()
        await task
        await services.close()
