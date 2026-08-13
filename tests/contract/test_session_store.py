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


async def test_update_with_empty_postimage_preserves_the_recorded_one(
    store: SessionStorePort,
) -> None:
    """Regression contract for a real drift: the two stores once disagreed on
    whether CONFIRMED with an empty postimage wipes the expectation recorded
    at STARTED. The contract is: an empty postimage on update means "keep
    what the record already holds" — a delete's empty expectation stays
    empty, and a patch effect confirmed without a digest keeps the expected
    one journaled at STARTED (recovery classifies against it)."""
    await store.record_execution(
        ExecutionRecord(
            call_id="c-keep",
            run_id="run-1",
            ticket_digest="t1",
            tool_name="repo.edit",
            effect_state=EffectState.STARTED,
            preimage_digest="pre",
            postimage_digest="expected-post",
            path="src/a.py",
        )
    )
    await store.update_execution_state("c-keep", EffectState.CONFIRMED, "")
    loaded = {r.call_id: r for r in await store.load_executions("run-1")}
    assert loaded["c-keep"].effect_state is EffectState.CONFIRMED
    assert loaded["c-keep"].postimage_digest == "expected-post"

    # And a non-empty postimage on update still overwrites.
    await store.update_execution_state("c-keep", EffectState.CONFIRMED, "actual-post")
    loaded = {r.call_id: r for r in await store.load_executions("run-1")}
    assert loaded["c-keep"].postimage_digest == "actual-post"


async def test_dest_path_roundtrips_for_move_records(store: SessionStorePort) -> None:
    """dest_path is what lets recovery classify an interrupted move end-to-end
    (ADR 0018 follow-up); both stores must persist and return it."""
    await store.record_execution(
        ExecutionRecord(
            call_id="c-move",
            run_id="run-1",
            ticket_digest="t2",
            tool_name="repo.move",
            effect_state=EffectState.STARTED,
            preimage_digest="pre",
            postimage_digest="",
            path="src/old.py",
            dest_path="src/new.py",
        )
    )
    loaded = {r.call_id: r for r in await store.load_executions("run-1")}
    assert loaded["c-move"].dest_path == "src/new.py"
    # State updates must not lose the destination.
    await store.update_execution_state("c-move", EffectState.EFFECT_UNKNOWN)
    loaded = {r.call_id: r for r in await store.load_executions("run-1")}
    assert loaded["c-move"].dest_path == "src/new.py"


async def test_artifact_roundtrip(store: SessionStorePort) -> None:
    digest = await store.put_artifact(b"large diff content")
    assert await store.get_artifact(digest) == b"large diff content"
    assert await store.get_artifact("missing" * 8) is None


async def test_delete_run_removes_every_trace(store: SessionStorePort) -> None:
    """gc's primitive: one call removes the run row, its events, checkpoint,
    approvals, and execution journal — and leaves other runs untouched."""
    for run_id in ("run-1", "run-2"):
        await store.create_run(run_id, "/tmp/ws", "d", "goal", "interactive")
        await store.append_event(run_id, StepStarted(run_id=run_id, step=1))
        await store.save_checkpoint(checkpoint(run_id=run_id))
        await store.record_approval(f"apr-{run_id}", run_id, "digest")
        await store.record_execution(
            ExecutionRecord(
                call_id=f"c-{run_id}",
                run_id=run_id,
                ticket_digest="t",
                tool_name="repo.edit",
                effect_state=EffectState.STARTED,
                preimage_digest="pre",
                postimage_digest="post",
                path="src/a.py",
            )
        )

    await store.delete_run("run-1")

    assert await store.get_run("run-1") is None
    assert await store.load_events("run-1") == []
    assert await store.load_checkpoint("run-1") is None
    assert await store.load_executions("run-1") == []
    # the other run is fully intact
    assert await store.get_run("run-2") is not None
    assert len(await store.load_events("run-2")) == 1
    assert await store.load_checkpoint("run-2") is not None
    assert len(await store.load_executions("run-2")) == 1


async def test_artifact_listing_and_deletion(store: SessionStorePort) -> None:
    d1 = await store.put_artifact(b"one")
    d2 = await store.put_artifact(b"two")
    listed = await store.list_artifacts()
    assert d1 in listed and d2 in listed

    await store.delete_artifact(d1)
    assert await store.get_artifact(d1) is None
    assert d1 not in await store.list_artifacts()
    # deleting a missing digest is a no-op, and path-shaped names are refused
    await store.delete_artifact(d1)
    await store.delete_artifact("../escape")
    assert await store.get_artifact(d2) == b"two"


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

    async def test_v1_database_migrates_in_place(self, tmp_path: Path) -> None:
        """A v1 store (no dest_path column) must open, gain the column, and
        read back records written before the migration."""
        import aiosqlite

        from haven.adapters.sqlite_session import DB_SCHEMA_VERSION

        db_path, art = tmp_path / "haven.db", tmp_path / "artifacts"
        # Build a genuine v1 database by hand: the v1 executions shape, v1 meta.
        raw = await aiosqlite.connect(db_path)
        await raw.executescript(
            """
            CREATE TABLE schema_meta (version INTEGER NOT NULL, migrated_at TEXT NOT NULL);
            INSERT INTO schema_meta VALUES (1, '2026-01-01T00:00:00');
            CREATE TABLE runs (id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                workspace_digest TEXT NOT NULL, goal TEXT NOT NULL, mode TEXT NOT NULL,
                status TEXT NOT NULL, stop_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE events (run_id TEXT NOT NULL, seq INTEGER NOT NULL,
                kind TEXT NOT NULL, schema_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY (run_id, seq));
            CREATE TABLE checkpoints (run_id TEXT NOT NULL, seq INTEGER NOT NULL,
                state_json TEXT NOT NULL, checksum TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY (run_id, seq));
            CREATE TABLE approvals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                request_digest TEXT NOT NULL, decision TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, decided_at TEXT, consumed_at TEXT);
            CREATE TABLE executions (call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                ticket_digest TEXT NOT NULL, tool_name TEXT NOT NULL,
                effect_state TEXT NOT NULL, preimage_digest TEXT NOT NULL DEFAULT '',
                postimage_digest TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO executions VALUES ('c1', 'run-1', 't', 'repo.edit', 'started',
                'pre', '', 'src/a.py', '2026-01-01T00:00:00', '2026-01-01T00:00:00');
            """
        )
        await raw.commit()
        await raw.close()

        store = await SqliteSessionStore.open(db_path, art)
        records = await store.load_executions("run-1")
        assert records[0].dest_path == ""
        cursor = await store._db.execute("SELECT version FROM schema_meta")  # type: ignore[attr-defined]  # noqa: SLF001
        row = await cursor.fetchone()
        assert row is not None and int(row["version"]) == DB_SCHEMA_VERSION
        await store.close()


def _unused(*args: Any) -> None:  # pragma: no cover
    pass
