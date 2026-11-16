"""选择检查组和观察真实门禁状态；活跃任务不接受降级。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.adapters.sqlite_session import SqliteSessionStore
from haven.application.verification import select_verification
from haven.contracts.checkpoint import CheckEvidenceSnapshot, EvidenceSnapshot
from haven.contracts.events import EventEnvelope, EvidenceRecorded, RunCreated
from haven.contracts.model import ModelEvent, ModelRequest
from haven.contracts.verification import CheckResultData
from haven.domain.enums import PermissionMode, RunStatus
from haven.interfaces.tui.app import ApprovalScreen, HavenApp
from haven.interfaces.tui.presenter import PresenterState, reduce
from haven.interfaces.tui.rendering import panel_text
from tests.contract.test_session_store import checkpoint
from tests.integration.fakes import RecordingLauncher
from tests.integration.harness import finish, make_repo, text
from tests.integration.test_verification_groups import check_turn, recipes
from tests.integration.test_verification_groups_cli import configure
from tests.tui.test_tui_journey import _approve_pending, _settle, _submit, _wait_ready


def builder(database: Path, model: ScriptedModel):  # type: ignore[no-untyped-def]
    async def build(*, workspace: Path, **kwargs: Any) -> bootstrap.AppServices:
        return await bootstrap.build_services(
            workspace,
            model=model,
            store_path=database,
            mode=PermissionMode.INTERACTIVE,
            worktree=False,
            **kwargs,
        )

    return build


async def test_select_and_clear_are_explicit_and_apply_only_to_new_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    repo = make_repo(tmp_path)
    configure(repo)
    model = ScriptedModel(
        [check_turn("test"), check_turn("lint", "lint"), [text("Verified."), finish()]],
        repeat_last=True,
    )
    app = HavenApp(workspace=repo, services_builder=builder(tmp_path / "sessions.db", model))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "/verify use quality")
        assert app._selected_check_group == "quality"
        assert model.requests_seen == []
        await _submit(app, pilot, "/verify")
        assert "test, lint" in app._state.timeline[-1].text
        await _submit(app, pilot, "Verify the project")
        await _approve_pending(app, pilot, "a", 2)
        await _settle(pilot, 20)
        assert app._state.status == "succeeded"
        evidence = panel_text(app._state).evidence
        assert "test: passed" in evidence and "lint: passed" in evidence
        first_id = app._state.run_id
        first = await app._svc.store.load_checkpoint(first_id)
        await _submit(app, pilot, "/verify clear")
        assert app._selected_check_group == ""
        assert app._state.verification is not None  # 旧任务显示仍对应原范围。
        await _submit(app, pilot, "Only explain this time")
        await _settle(pilot, 15)
        assert app._state.status == "succeeded" and app._state.verification is None
        assert await app._svc.store.load_checkpoint(first_id) == first
        assert await app._svc.store.load_input_history(str(repo.resolve())) == [
            "Verify the project",
            "Only explain this time",
        ]


async def test_running_task_rejects_a_selection_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    repo = make_repo(tmp_path)
    configure(repo)
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            entered.set()
            await release.wait()
            async for event in super().generate_stream(request):
                yield event

    model = WaitingModel(
        [check_turn("test"), check_turn("lint", "lint"), [text("Verified."), finish()]]
    )
    app = HavenApp(workspace=repo, services_builder=builder(tmp_path / "sessions.db", model))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "/verify use quality")
        await _submit(app, pilot, "Verify the project")
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await _submit(app, pilot, "/verify clear")
            assert app._selected_check_group == "quality"
            assert "before changing required checks" in app._state.timeline[-1].text
        finally:
            release.set()
        await _approve_pending(app, pilot, "a", 2)
        await _settle(pilot, 20)
        assert app._state.status == "succeeded"
        assert app._state.verification is not None


async def test_quitting_during_required_check_approval_saves_cancellation_before_closing_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "select_launcher", lambda: RecordingLauncher())
    repo = make_repo(tmp_path)
    configure(repo)
    database = tmp_path / "sessions.db"
    model = ScriptedModel([check_turn("test")])
    app = HavenApp(workspace=repo, services_builder=builder(database, model))
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "/verify use quality")
        await _submit(app, pilot, "Verify the project")
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, ApprovalScreen):
                break
        assert isinstance(app.screen, ApprovalScreen)
        run_id = app._state.run_id
        assert len(model.requests_seen) == 1
    reopened = await SqliteSessionStore.open(database, tmp_path / "artifacts")
    try:
        run = await reopened.get_run(run_id)
        assert run is not None and run.status is RunStatus.CANCELLED
        checkpoint = await reopened.load_checkpoint(run_id)
        assert checkpoint is not None and checkpoint.verification is not None
        assert checkpoint.verification.group_name == "quality"
        assert checkpoint.evidence.checks == ()
        assert await reopened.load_executions(run_id) == []
    finally:
        await reopened.close()


async def test_opening_an_interrupted_session_uses_saved_evidence_not_later_history(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path)
    configure(repo)
    app = HavenApp(
        workspace=repo,
        services_builder=builder(tmp_path / "sessions.db", ScriptedModel([])),
    )
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        service = app._svc.run_service
        selection = select_verification("quality", service.verification_groups, recipes())
        assert selection is not None
        await app._svc.store.create_run(
            "interrupted", str(repo), service.workspace_digest, "Verify", "interactive"
        )
        await app._svc.emitter.emit(
            "interrupted",
            RunCreated(
                run_id="interrupted",
                workspace=str(repo),
                workspace_digest=service.workspace_digest,
                goal="Verify",
                mode="interactive",
                model_name="scripted",
                verification=selection,
            ),
        )
        unit = CheckResultData(
            recipe_id="test",
            recipe_digest=selection.required[0].recipe_digest,
            exit_code=0,
            duration_ms=1,
            truncated=False,
        )
        await app._svc.emitter.emit(
            "interrupted",
            EvidenceRecorded(
                run_id="interrupted",
                evidence_kind="check",
                summary="test passed",
                check=unit,
            ),
        )
        saved = checkpoint("interrupted", last_seq=2).model_copy(
            update={
                "workspace_digest": service.workspace_digest,
                "verification": selection,
                "evidence": EvidenceSnapshot(
                    checks=(CheckEvidenceSnapshot(seq=2, **unit.model_dump()),)
                ),
            }
        )
        await app._svc.store.save_checkpoint(saved)
        await app._svc.emitter.emit(
            "interrupted",
            EvidenceRecorded(
                run_id="interrupted",
                evidence_kind="check",
                summary="lint passed after checkpoint",
                check=CheckResultData(
                    recipe_id="lint",
                    recipe_digest=selection.required[1].recipe_digest,
                    exit_code=0,
                    duration_ms=1,
                    truncated=False,
                ),
            ),
        )
        assert await app._load_session("interrupted")
        panel = panel_text(app._state).evidence
        assert "test: passed" in panel
        assert "lint: not_run" in panel
        assert any("lint passed after checkpoint" in row for row in app._state.evidence_rows)


def test_evidence_panel_distinguishes_missing_passed_failed_stale_and_mismatched_checks() -> None:
    selection = select_verification("quality", {"quality": ("test", "lint")}, recipes())
    assert selection is not None
    metadata = RunCreated(
        run_id="run",
        workspace="/repo",
        workspace_digest="ws",
        goal="verify",
        mode="interactive",
        model_name="scripted",
        verification=selection,
    )
    state = reduce(PresenterState(), EventEnvelope(seq=1, at="", event=metadata))
    assert "test: not_run" in panel_text(state).evidence

    def record(seq: int, recipe: str, code: int, digest: str | None = None) -> None:
        nonlocal state
        required = next(item for item in selection.required if item.recipe_id == recipe)
        state = reduce(
            state,
            EventEnvelope(
                seq=seq,
                at="",
                event=EvidenceRecorded(
                    run_id="run",
                    evidence_kind="check",
                    summary="check",
                    check=CheckResultData(
                        recipe_id=recipe,
                        recipe_digest=digest or required.recipe_digest,
                        exit_code=code,
                        duration_ms=1,
                        truncated=False,
                    ),
                ),
            ),
        )

    record(2, "test", 0)
    record(3, "lint", 1)
    assert "test: passed" in panel_text(state).evidence
    assert "lint: failed" in panel_text(state).evidence
    record(4, "lint", 0)
    assert "lint: passed" in panel_text(state).evidence
    state = reduce(
        state,
        EventEnvelope(
            seq=5,
            at="",
            event=EvidenceRecorded(
                run_id="run",
                evidence_kind="edit",
                summary="new write",
            ),
        ),
    )
    assert "test: stale" in panel_text(state).evidence
    assert "lint: stale" in panel_text(state).evidence
    record(6, "test", 0, "different command")
    assert "test: mismatch" in panel_text(state).evidence
