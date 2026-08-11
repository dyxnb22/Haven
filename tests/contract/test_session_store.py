"""Contract tests run against BOTH store implementations to keep them equal."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from haven.adapters.memory_session import MemorySessionStore
from haven.adapters.sqlite_session import SqliteSessionStore, StoreError
from haven.contracts.checkpoint import (
    BudgetSnapshot,
    CheckpointV1,
    EvidenceSnapshot,
    UsageSnapshot,
)
from haven.contracts.events import RunCreated, StepStarted
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.enums import ApprovalDecision, EffectState, RunStatus
from haven.ports.session import ExecutionRecord, SessionStorePort


@pytest.fixture(params=["sqlite", "memory"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[SessionStorePort]:
    if request.param == "sqlite":
        s = await SqliteSessionStore.open(tmp_path / "haven.db", tmp_path / "artifacts")
    else:
        s = MemorySessionStore()
    yield s
    await s.close()


def checkpoint(run_id: str = "run-1", last_seq: int = 3) -> CheckpointV1:
    return CheckpointV1(
        run_id=run_id,
        workspace_digest="ws",
        goal="fix bug",
        mode="interactive",
        status=RunStatus.RUNNING_MODEL.value,
        last_seq=last_seq,
        budget=BudgetSnapshot.from_domain(Budget()),
        usage=UsageSnapshot.from_domain(BudgetUsage(steps=2)),
        messages=(),
        evidence=EvidenceSnapshot(),
    )


async def test_run_lifecycle(store: SessionStorePort) -> None:
    await store.create_run("run-1", "/tmp/ws", "wsdigest", "fix bug", "interactive")
    record = await store.get_run("run-1")
    assert record is not None
    assert record.status is RunStatus.CREATED

    await store.update_run_status("run-1", RunStatus.SUCCEEDED, "evidence_satisfied")
    record = await store.get_run("run-1")
    assert record is not None
    assert record.status is RunStatus.SUCCEEDED
    assert record.stop_reason == "evidence_satisfied"

    runs = await store.list_runs(10)
    assert [r.run_id for r in runs] == ["run-1"]


async def test_event_journal_is_ordered_and_typed(store: SessionStorePort) -> None:
    await store.create_run("run-1", "/tmp/ws", "d", "goal", "interactive")
    e1 = await store.append_event(
        "run-1",
        RunCreated(
            run_id="run-1",
            workspace="/tmp/ws",
            workspace_digest="d",
            goal="goal",
            mode="interactive",
            model_name="scripted",
        ),
    )
    e2 = await store.append_event("run-1", StepStarted(run_id="run-1", step=1))
    assert (e1.seq, e2.seq) == (1, 2)

    loaded = await store.load_events("run-1")
    assert [env.seq for env in loaded] == [1, 2]
    assert isinstance(loaded[0].event, RunCreated)
    assert isinstance(loaded[1].event, StepStarted)


async def test_checkpoint_roundtrip(store: SessionStorePort) -> None:
    await store.save_checkpoint(checkpoint())
    loaded = await store.load_checkpoint("run-1")
    assert loaded is not None
    assert loaded.goal == "fix bug"
    assert loaded.usage.steps == 2


async def test_approval_single_consumption(store: SessionStorePort) -> None:
    await store.record_approval("apr-1", "run-1", "digest-1")
    await store.decide_approval("apr-1", ApprovalDecision.APPROVED)

    assert await store.consume_approval("apr-1", "digest-1") is True
    # second consumption must fail
    assert await store.consume_approval("apr-1", "digest-1") is False


async def test_approval_wrong_digest_rejected(store: SessionStorePort) -> None:
    await store.record_approval("apr-1", "run-1", "digest-1")
    await store.decide_approval("apr-1", ApprovalDecision.APPROVED)
    assert await store.consume_approval("apr-1", "digest-OTHER") is False


async def test_rejected_approval_cannot_be_consumed(store: SessionStorePort) -> None:
    await store.record_approval("apr-1", "run-1", "digest-1")
    await store.decide_approval("apr-1", ApprovalDecision.REJECTED)
    assert await store.consume_approval("apr-1", "digest-1") is False


async def test_execution_journal(store: SessionStorePort) -> None:
    record = ExecutionRecord(
        call_id="c1",
        run_id="run-1",
        ticket_digest="t1",
        tool_name="repo.edit",
        effect_state=EffectState.STARTED,
        preimage_digest="pre",
        postimage_digest="",
        path="src/a.py",
    )
    await store.record_execution(record)
    await store.update_execution_state("c1", EffectState.CONFIRMED, "post")

    loaded = await store.load_executions("run-1")
    assert len(loaded) == 1
    assert loaded[0].effect_state is EffectState.CONFIRMED
    assert loaded[0].postimage_digest == "post"


async def test_artifact_roundtrip(store: SessionStorePort) -> None:
    digest = await store.put_artifact(b"large diff content")
    assert await store.get_artifact(digest) == b"large diff content"
    assert await store.get_artifact("missing" * 8) is None


class TestSqliteSpecific:
    async def test_events_survive_reopen(self, tmp_path: Path) -> None:
        db, art = tmp_path / "haven.db", tmp_path / "artifacts"
        store = await SqliteSessionStore.open(db, art)
        await store.create_run("run-1", "/tmp/ws", "d", "goal", "interactive")
        await store.append_event("run-1", StepStarted(run_id="run-1", step=1))
        await store.close()

        reopened = await SqliteSessionStore.open(db, art)
        events = await reopened.load_events("run-1")
        assert len(events) == 1
        # seq continues after the persisted journal
        env = await reopened.append_event("run-1", StepStarted(run_id="run-1", step=2))
        assert env.seq == 2
        await reopened.close()

    async def test_corrupted_event_fails_closed(self, tmp_path: Path) -> None:
        db, art = tmp_path / "haven.db", tmp_path / "artifacts"
        store = await SqliteSessionStore.open(db, art)
        await store.create_run("run-1", "/tmp/ws", "d", "goal", "interactive")
        await store.append_event("run-1", StepStarted(run_id="run-1", step=1))
        # tamper with the journal
        await store._db.execute(  # type: ignore[attr-defined]  # noqa: SLF001
            "UPDATE events SET payload_json = ? WHERE run_id = 'run-1'",
            ('{"kind": "step.started", "run_id": "run-1", "step": 99}',),
        )
        await store._db.commit()  # type: ignore[attr-defined]  # noqa: SLF001
        with pytest.raises(StoreError, match="digest"):
            await store.load_events("run-1")
        await store.close()

    async def test_schema_version_mismatch_fails_closed(self, tmp_path: Path) -> None:
        db, art = tmp_path / "haven.db", tmp_path / "artifacts"
        store = await SqliteSessionStore.open(db, art)
        await store._db.execute("UPDATE schema_meta SET version = 999")  # type: ignore[attr-defined]  # noqa: SLF001
        await store._db.commit()  # type: ignore[attr-defined]  # noqa: SLF001
        await store.close()

        with pytest.raises(StoreError, match="schema"):
            await SqliteSessionStore.open(db, art)


def _unused(*args: Any) -> None:  # pragma: no cover
    pass
