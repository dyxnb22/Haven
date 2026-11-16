"""日志构件按运行绑定、验证完整性，并跟随保留策略清理。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from haven.adapters.memory_session import MemorySessionStore
from haven.adapters.sqlite_session import SqliteSessionStore
from haven.application.maintenance import collect_garbage
from haven.application.process_outputs import read_process_output, save_process_output
from haven.contracts.events import EVENT_ADAPTER, ToolCompleted
from haven.contracts.process_output import ProcessReport
from haven.domain.enums import RunStatus
from haven.ports.executor import CapturedOutput, ExecOutcome
from tests.unit.test_maintenance import _seed_run


async def completed_output(
    store: MemorySessionStore | SqliteSessionStore, run_id: str
) -> ToolCompleted:
    await _seed_run(store, run_id)
    outcome = ExecOutcome(1, 1, "tail", "", True, False, CapturedOutput("EARLY\nTAIL", "stderr"))
    report = await save_process_output(
        store, outcome, Path("/workspace"), run_id=run_id, call_id="check"
    )
    event = ToolCompleted(
        run_id=run_id, call_id="check", tool_name="repo.exec", status="ok", process=report
    )
    await store.append_event(run_id, event)
    return event


async def test_output_survives_sqlite_reopen_and_missing_or_corrupt_artifacts_are_visible(
    tmp_path: Path,
) -> None:
    database, artifacts = tmp_path / "sessions.db", tmp_path / "artifacts"
    store = await SqliteSessionStore.open(database, artifacts)
    event = await completed_output(store, "run")
    await store.close()
    store = await SqliteSessionStore.open(database, artifacts)
    try:
        assert "EARLY\nTAIL" in await read_process_output(store, "run", "check")
        assert event.process is not None
        path = artifacts / event.process.output_digest
        original = path.read_bytes()
        path.write_bytes(b"corrupt")
        from haven.ports.session import ArtifactError

        with pytest.raises(ArtifactError, match="digest verification"):
            await read_process_output(store, "run", "check")
        path.write_bytes(original)
        path.unlink()
        with pytest.raises(ValueError, match="missing"):
            await read_process_output(store, "run", "check")
    finally:
        await store.close()


async def test_gc_keeps_live_event_references_and_deletes_them_with_their_run() -> None:
    store = MemorySessionStore()
    event = await completed_output(store, "old")
    assert event.process is not None
    await _seed_run(store, "new")
    await collect_garbage(store, keep=2, apply=True)
    assert "EARLY" in await read_process_output(store, "old", "check")
    report = await collect_garbage(store, keep=1, apply=True)
    assert report.deleted == ("old",)
    assert await store.get_artifact(event.process.output_digest) is None


async def test_active_runs_protect_artifacts_not_yet_linked_to_a_completed_event() -> None:
    store = MemorySessionStore()
    await _seed_run(store, "active", status=RunStatus.EXECUTING_TOOL)
    digest = await store.put_artifact(b"log being persisted")
    report = await collect_garbage(store, keep=0, apply=True)
    assert await store.get_artifact(digest) == b"log being persisted"
    assert "artifact sweep deferred" in " ".join(report.notes)
    await store.update_run_status("active", RunStatus.CANCELLED, "cancelled")
    await collect_garbage(store, keep=0, apply=True)
    assert await store.get_artifact(digest) is None


async def test_gc_does_not_sweep_output_created_after_its_run_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemorySessionStore()
    await completed_output(store, "existing")
    load = store.load_events

    async def concurrent_start(run_id: str):
        if await store.get_run("new") is None:
            await completed_output(store, "new")
        return await load(run_id)

    monkeypatch.setattr(store, "load_events", concurrent_start)
    await collect_garbage(store, keep=1, apply=True)
    assert "EARLY" in await read_process_output(store, "new", "check")


def test_old_events_remain_readable_and_output_fields_are_bounded() -> None:
    event = ToolCompleted(run_id="r", call_id="c", tool_name="repo.read", status="ok")
    assert "process" not in event.model_dump(mode="json")
    assert EVENT_ADAPTER.validate_json(event.model_dump_json()).process is None
    for digest, diagnostics in (("../file", ()), ("a" * 64, ("x" * 601,))):
        with pytest.raises(ValidationError):
            ProcessReport(
                cwd="/workspace", exit_code=1, output_digest=digest, diagnostics=diagnostics
            )
