"""针对两个存储实现运行的契约测试，以保持二者行为一致。"""

import asyncio
import json
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import closing
from pathlib import Path

import pytest

from haven.adapters.memory_session import MemorySessionStore
from haven.adapters.sqlite_session import SqliteSessionStore, StoreError
from haven.contracts.checkpoint import (
    BudgetSnapshot,
    CheckpointV1,
    EvidenceSnapshot,
    UsageSnapshot,
)
from haven.contracts.events import HandoffUpdated, RunCreated, StepStarted
from haven.contracts.handoff import HandoffNotes
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.digest import sha256_text
from haven.domain.enums import AgentMode, ApprovalDecision, EffectState, RunStatus
from haven.ports.session import ExecutionRecord, SessionStorePort


@pytest.fixture(params=["sqlite", "memory"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[SessionStorePort]:
    s: SessionStorePort
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

    await store.create_run("run-2", "/tmp/ws", "wsdigest", "other", "interactive")
    await store.update_run_status("run-1", RunStatus.SUCCEEDED, "evidence_satisfied")
    runs = await store.list_runs(10)
    assert [r.run_id for r in runs] == ["run-1", "run-2"]
    assert await store.list_runs(0) == []
    with pytest.raises(ValueError, match="non-negative"):
        await store.list_runs(-1)


async def test_handoff_revision_is_atomic_and_leaves_checkpoint_and_status_untouched(
    store: SessionStorePort,
) -> None:
    await store.create_run("run-1", "/tmp/ws", "ws", "task", "interactive")
    await store.append_event("run-1", StepStarted(run_id="run-1", step=1))
    saved = checkpoint(last_seq=1)
    await store.save_checkpoint(saved)
    await store.update_run_status("run-1", RunStatus.WAITING_USER, "needs_input")
    event = HandoffUpdated(
        run_id="run-1",
        notes=HandoffNotes(decisions="Keep the existing API: callers depend on it"),
        checkpoint_seq=1,
    )
    outcomes = await asyncio.gather(
        store.append_handoff(event, 1), store.append_handoff(event, 1), return_exceptions=True
    )
    assert sum(isinstance(item, ValueError) for item in outcomes) == 1
    assert (await store.load_events("run-1"))[-1].seq == 2
    assert await store.load_checkpoint("run-1") == saved
    record = await store.get_run("run-1")
    assert record is not None and record.status is RunStatus.WAITING_USER
    cleared = await store.append_handoff(event.model_copy(update={"notes": None}), 2)
    assert cleared.seq == 3
    assert await store.load_checkpoint("run-1") == saved


async def test_handoff_refuses_active_or_delegated_runs(store: SessionStorePort) -> None:
    await store.create_run("run-1", "/tmp/ws", "ws", "task", "interactive")
    await store.save_checkpoint(checkpoint(last_seq=0))
    event = HandoffUpdated(
        run_id="run-1", notes=HandoffNotes(next_steps="Inspect"), checkpoint_seq=0
    )
    with pytest.raises(ValueError, match="run is active"):
        await store.append_handoff(event, 0)
    await store.update_run_status("run-1", RunStatus.STOPPED, "stopped")
    await store.save_checkpoint(
        checkpoint(last_seq=0).model_copy(update={"parent_run_id": "parent"})
    )
    with pytest.raises(ValueError, match="run is active"):
        await store.append_handoff(event, 0)
    assert await store.load_events("run-1") == []


async def test_external_handoff_writers_cannot_reuse_event_sequences(tmp_path: Path) -> None:
    first = await SqliteSessionStore.open(tmp_path / "store.db", tmp_path / "artifacts")
    second = await SqliteSessionStore.open(tmp_path / "store.db", tmp_path / "artifacts")
    try:
        await first.create_run("run-1", "/tmp/ws", "ws", "task", "interactive")
        await first.append_event("run-1", StepStarted(run_id="run-1", step=1))
        await first.save_checkpoint(checkpoint(last_seq=1))
        await first.update_run_status("run-1", RunStatus.STOPPED, "stopped")
        event = HandoffUpdated(
            run_id="run-1", notes=HandoffNotes(decisions="Decision"), checkpoint_seq=1
        )
        results = await asyncio.gather(
            first.append_handoff(event, 1), second.append_handoff(event, 1), return_exceptions=True
        )
        assert sum(isinstance(item, ValueError) for item in results) == 1
        await asyncio.gather(
            first.append_event("run-1", StepStarted(run_id="run-1", step=2)),
            second.append_event("run-1", StepStarted(run_id="run-1", step=3)),
        )
        assert [item.seq for item in await second.load_events("run-1")] == [1, 2, 3, 4]
    finally:
        await first.close()
        await second.close()


async def test_session_search_matches_full_history_and_literal_patterns(
    store: SessionStorePort,
) -> None:
    await store.create_run("older", "/ws", "same", "fix 100%_ready", "interactive")
    for index in range(12):
        await store.create_run(f"new-{index}", "/ws", "same", "recent", "interactive")
    await store.create_run("elsewhere", "/other", "different", "100%_ready", "interactive")
    await store.create_run("child-123", "/ws", "same", "100%_ready", "read_only")
    assert [r.run_id for r in await store.search_runs("%_", 2, "same")] == ["older"]
    assert [r.run_id for r in await store.search_runs("OLDER", 2)] == ["older"]
    await store.update_run_status("older", RunStatus.SUCCEEDED, "done")
    assert [r.run_id for r in await store.search_runs("succeeded", 1)] == ["older"]
    assert await store.search_runs("", 0) == []
    with pytest.raises(ValueError, match="non-negative"):
        await store.search_runs("", -1)


async def test_input_history_is_bounded_ordered_and_workspace_scoped(
    store: SessionStorePort,
) -> None:
    assert await store.load_input_history("/first") == []
    await store.record_input("/other", "another workspace's task")
    for index in range(25):
        await store.record_input("/first", f"任务 {index}: inspect @src/parser.py")
    expected = [f"任务 {index}: inspect @src/parser.py" for index in range(5, 25)]
    assert await store.load_input_history("/first") == expected
    assert await store.load_input_history("/other") == ["another workspace's task"]
    loaded = await store.load_input_history("/first")
    loaded.clear()
    assert await store.load_input_history("/first") == expected
    assert await store.list_runs(20) == []


async def test_clearing_input_history_preserves_other_workspaces_and_runs(
    store: SessionStorePort,
) -> None:
    await store.create_run("run-1", "/first", "ws", "saved goal", "interactive")
    await store.save_checkpoint(checkpoint())
    await store.record_input("/first", "clear this task")
    await store.record_input("/other", "keep this task")
    await store.clear_input_history("/first")
    await store.clear_input_history("/first")
    assert await store.load_input_history("/first") == []
    assert await store.load_input_history("/other") == ["keep this task"]
    assert await store.get_run("run-1") is not None
    assert await store.load_checkpoint("run-1") == checkpoint()


async def test_v4_database_adds_input_history_without_losing_runs(tmp_path: Path) -> None:
    from haven.adapters.sqlite_session import DB_SCHEMA_VERSION

    db, artifacts = tmp_path / "sessions.db", tmp_path / "artifacts"
    original = await SqliteSessionStore.open(db, artifacts)
    await original.create_run("saved-run", "/ws", "digest", "original goal", "interactive")
    # v4 与当前 runs 结构一致，但没有输入历史表。
    await original._db.execute("DROP TABLE input_history")
    await original._db.execute("UPDATE schema_meta SET version = 4")
    await original._db.commit()
    await original.close()

    migrated = await SqliteSessionStore.open(db, artifacts)
    try:
        run = await migrated.get_run("saved-run")
        assert run is not None and run.goal == "original goal"
        assert await migrated.load_input_history("/ws") == []
        await migrated.record_input("/ws", "a new task")
        cursor = await migrated._db.execute("SELECT version FROM schema_meta")
        row = await cursor.fetchone()
        assert row is not None and row["version"] == DB_SCHEMA_VERSION
    finally:
        await migrated.close()

    reopened = await SqliteSessionStore.open(db, artifacts)
    try:
        assert await reopened.load_input_history("/ws") == ["a new task"]
    finally:
        await reopened.close()


async def test_project_notes_are_scoped_and_separate_from_runs_and_history(
    store: SessionStorePort,
) -> None:
    await store.create_run("run-1", "/first", "ws", "saved goal", "interactive")
    await store.save_checkpoint(checkpoint())
    await store.record_input("/first", "a previous task")
    first = await store.add_project_note("/first", "  项目命令：uv run pytest  ")
    other = await store.add_project_note("/other", "another project's note")
    assert first.text == "项目命令：uv run pytest"
    assert first.id != other.id and first.created_at.endswith("Z")
    assert await store.list_project_notes("/first") == (first,)
    assert not await store.delete_project_note("/first", other.id)
    assert not await store.delete_project_note("/first", -1)
    assert await store.delete_project_note("/first", first.id)
    assert not await store.delete_project_note("/first", first.id)
    await store.clear_project_notes("/first")
    await store.clear_project_notes("/first")
    assert await store.list_project_notes("/first") == ()
    assert await store.list_project_notes("/other") == (other,)
    assert await store.load_input_history("/first") == ["a previous task"]
    assert await store.get_run("run-1") is not None
    assert await store.load_checkpoint("run-1") == checkpoint()


@pytest.mark.parametrize("text", ["", "  ", "x" * 501, "note\x00hidden"])
async def test_invalid_project_notes_do_not_write(store: SessionStorePort, text: str) -> None:
    with pytest.raises(ValueError, match="1–500"):
        await store.add_project_note("/first", text)
    assert await store.list_project_notes("/first") == ()


async def test_project_note_capacity_rejects_new_entries_without_evicting_old_ones(
    store: SessionStorePort,
) -> None:
    for _ in range(20):
        await store.add_project_note("/first", "short note")
    original = await store.list_project_notes("/first")
    with pytest.raises(ValueError, match="full"):
        await store.add_project_note("/first", "one too many")
    assert await store.list_project_notes("/first") == original
    await store.clear_project_notes("/first")
    for _ in range(4):
        await store.add_project_note("/first", "约" * 500)
    original = await store.list_project_notes("/first")
    with pytest.raises(ValueError, match="full"):
        await store.add_project_note("/first", "界")
    assert await store.list_project_notes("/first") == original
    assert await store.delete_project_note("/first", original[0].id)
    await store.add_project_note("/first", "now there is room")
    assert len(await store.list_project_notes("/first")) == 4


async def test_v5_migration_adds_notes_and_preserves_history_and_checkpoints(
    tmp_path: Path,
) -> None:
    from haven.adapters.sqlite_session import DB_SCHEMA_VERSION

    db, artifacts = tmp_path / "sessions.db", tmp_path / "artifacts"
    old = await SqliteSessionStore.open(db, artifacts)
    await old.create_run("run-1", "/ws", "digest", "saved goal", "interactive")
    await old.save_checkpoint(checkpoint())
    await old.record_input("/ws", "saved task input")
    await old._db.execute("DROP TABLE project_notes")
    await old._db.execute("UPDATE schema_meta SET version = 5")
    await old._db.commit()
    await old.close()
    migrated = await SqliteSessionStore.open(db, artifacts)
    try:
        assert await migrated.list_project_notes("/ws") == ()
        note = await migrated.add_project_note("/ws", "keep this after restarting")
        assert await migrated.load_input_history("/ws") == ["saved task input"]
        assert await migrated.load_checkpoint("run-1") == checkpoint()
        cursor = await migrated._db.execute("SELECT version FROM schema_meta")
        row = await cursor.fetchone()
        assert row is not None and row["version"] == DB_SCHEMA_VERSION
    finally:
        await migrated.close()
    reopened = await SqliteSessionStore.open(db, artifacts)
    try:
        assert await reopened.list_project_notes("/ws") == (note,)
        assert await reopened.delete_project_note("/ws", note.id)
    finally:
        await reopened.close()
    again = await SqliteSessionStore.open(db, artifacts)
    try:
        assert await again.list_project_notes("/ws") == ()
        assert await again.get_run("run-1") is not None
    finally:
        await again.close()


async def test_concurrent_connections_cannot_overfill_project_notes(tmp_path: Path) -> None:
    db, artifacts = tmp_path / "sessions.db", tmp_path / "artifacts"
    first = await SqliteSessionStore.open(db, artifacts)
    second = await SqliteSessionStore.open(db, artifacts)
    try:
        attempts = await asyncio.gather(
            *(
                (first if index % 2 else second).add_project_note("/ws", f"note {index}")
                for index in range(30)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ValueError) for result in attempts) == 10
        assert all(
            not isinstance(result, Exception) or isinstance(result, ValueError)
            for result in attempts
        )
        assert len(await first.list_project_notes("/ws")) == 20
    finally:
        await first.close()
        await second.close()


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

    concurrent = await asyncio.gather(
        *(
            store.append_event("run-1", StepStarted(run_id="run-1", step=step))
            for step in range(3, 13)
        )
    )
    assert sorted(envelope.seq for envelope in concurrent) == list(range(3, 13))
    assert [envelope.seq for envelope in await store.load_events("run-1")] == list(range(1, 13))


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
    # 第二次消耗必须失败
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
    await store.record_execution(
        ExecutionRecord(
            call_id="c1",
            run_id="run-2",
            ticket_digest="t2",
            tool_name="repo.check",
            effect_state=EffectState.STARTED,
            preimage_digest="",
            postimage_digest="",
            path="",
        )
    )
    await store.update_execution_state("run-1", "c1", EffectState.CONFIRMED, "post")

    loaded = await store.load_executions("run-1")
    assert len(loaded) == 1
    assert loaded[0].effect_state is EffectState.CONFIRMED
    assert loaded[0].postimage_digest == "post"
    other = await store.load_executions("run-2")
    assert len(other) == 1
    assert other[0].effect_state is EffectState.STARTED


async def test_successive_checkpoints_resolve_to_the_newest(store: SessionStorePort) -> None:
    """运行每个工具批次保存一次检查点；无论底层使用哪个存储，恢复都必须落到最新
    快照。"""
    for seq in (1, 5, 9):
        await store.save_checkpoint(checkpoint(last_seq=seq))
    loaded = await store.load_checkpoint("run-1")
    assert loaded is not None
    assert loaded.last_seq == 9


async def test_an_out_of_order_save_does_not_lose_the_newer_checkpoint(
    store: SessionStorePort,
) -> None:
    """清理已被替代的行时绝不能删除比正在写入的行更*靠前*的行——否则恢复会悄悄
    倒退。"""
    await store.save_checkpoint(checkpoint(last_seq=9))
    await store.save_checkpoint(checkpoint(last_seq=4))
    loaded = await store.load_checkpoint("run-1")
    assert loaded is not None
    assert loaded.last_seq == 9


async def test_update_with_empty_postimage_preserves_the_recorded_one(
    store: SessionStorePort,
) -> None:
    """针对一次真实漂移的回归契约：两个存储曾经对“空 postimage 的 CONFIRMED 是否
    清除 STARTED 时记录的预期”意见不一。契约规定：更新时空 postimage 表示“保留
    记录已有内容”——删除操作的空预期仍为空，而没有摘要就确认的补丁效果保留
    STARTED 时写入日志的预期值（恢复会据此分类）。"""
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
    await store.update_execution_state("run-1", "c-keep", EffectState.CONFIRMED, "")
    loaded = {r.call_id: r for r in await store.load_executions("run-1")}
    assert loaded["c-keep"].effect_state is EffectState.CONFIRMED
    assert loaded["c-keep"].postimage_digest == "expected-post"

    # update 时非空 postimage 仍然会覆盖原值。
    await store.update_execution_state("run-1", "c-keep", EffectState.CONFIRMED, "actual-post")
    loaded = {r.call_id: r for r in await store.load_executions("run-1")}
    assert loaded["c-keep"].postimage_digest == "actual-post"


async def test_dest_path_roundtrips_for_move_records(store: SessionStorePort) -> None:
    """dest_path 使恢复能够端到端地分类中断的移动操作（ADR 0018 后续）；两个存储
    都必须持久化并返回它。"""
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
    # 状态更新不能丢失目标路径。
    await store.update_execution_state("run-1", "c-move", EffectState.EFFECT_UNKNOWN)
    loaded = {r.call_id: r for r in await store.load_executions("run-1")}
    assert loaded["c-move"].dest_path == "src/new.py"


async def test_artifact_roundtrip(store: SessionStorePort) -> None:
    digest = await store.put_artifact(b"large diff content")
    assert await store.get_artifact(digest) == b"large diff content"
    assert await store.get_artifact("missing" * 8) is None
    if isinstance(store, SqliteSessionStore):
        (store._artifacts_dir / digest).write_bytes(b"corrupt")
        with pytest.raises(StoreError, match="digest verification"):
            await store.get_artifact(digest)
        # put is self-healing for a corrupted content-addressed object.
        assert await store.put_artifact(b"large diff content") == digest
        assert await store.get_artifact(digest) == b"large diff content"


async def test_delete_run_removes_every_trace(store: SessionStorePort) -> None:
    """gc 的基础操作：一次调用删除运行行、事件、检查点、审批和执行日志，同时不
    触碰其他运行。"""
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
    # 另一个运行完全不受影响
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
    # 删除不存在的摘要是空操作，而类似路径的名称会被拒绝
    await store.delete_artifact(d1)
    await store.delete_artifact("../escape")
    assert await store.get_artifact(d2) == b"two"


class TestSqliteSpecific:
    async def test_concurrent_fresh_opens_share_one_schema_version(self, tmp_path: Path) -> None:
        from haven.adapters.sqlite_session import DB_SCHEMA_VERSION

        db, artifacts = tmp_path / "haven.db", tmp_path / "artifacts"

        async def open_and_close() -> None:
            store = await SqliteSessionStore.open(db, artifacts)
            await store.close()

        await asyncio.gather(*(open_and_close() for _ in range(8)))
        with closing(sqlite3.connect(db)) as raw:
            assert raw.execute("SELECT version FROM schema_meta").fetchall() == [
                (DB_SCHEMA_VERSION,)
            ]

    async def test_legacy_checkpoint_defaults_do_not_invalidate_its_stored_checksum(
        self, tmp_path: Path
    ) -> None:
        db, art = tmp_path / "haven.db", tmp_path / "artifacts"
        store = await SqliteSessionStore.open(db, art)
        try:
            original = checkpoint().model_dump(mode="json")
            original.pop("agent_mode")
            original.pop("plan_status")
            original.pop("guidance_snapshot")
            original.pop("verification")
            payload = json.dumps(original, separators=(",", ":"), ensure_ascii=False)
            await store._db.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?)",
                ("run-1", 3, payload, sha256_text(payload), "2026-10-28T00:00:00Z"),
            )
            await store._db.commit()
        finally:
            await store.close()

        reopened = await SqliteSessionStore.open(db, art)
        try:
            loaded = await reopened.load_checkpoint("run-1")
            assert loaded is not None
            assert loaded.agent_mode is AgentMode.AGENT
            assert loaded.plan_status is None
            assert loaded.guidance_snapshot is None
            assert loaded.verification is None

            corrupted = payload.replace('"fix bug"', '"tampered goal"')
            await reopened._db.execute(
                "UPDATE checkpoints SET state_json = ? WHERE run_id = 'run-1'", (corrupted,)
            )
            await reopened._db.commit()
            with pytest.raises(StoreError, match="checksum"):
                await reopened.load_checkpoint("run-1")
        finally:
            await reopened.close()

    async def test_events_survive_reopen(self, tmp_path: Path) -> None:
        db, art = tmp_path / "haven.db", tmp_path / "artifacts"
        store = await SqliteSessionStore.open(db, art)
        await store.create_run("run-1", "/tmp/ws", "d", "goal", "interactive")
        await store.append_event("run-1", StepStarted(run_id="run-1", step=1))
        await store.close()

        reopened = await SqliteSessionStore.open(db, art)
        events = await reopened.load_events("run-1")
        assert len(events) == 1
        # seq 会在持久化日志之后继续递增
        env = await reopened.append_event("run-1", StepStarted(run_id="run-1", step=2))
        assert env.seq == 2
        await reopened.close()

    async def test_superseded_checkpoint_rows_are_pruned(self, tmp_path: Path) -> None:
        """上述行为契约背后的存储主张：每次运行只有一行存活记录，而不是每个工具
        批次一行。此前测得，被替代行占真实存储的 92%，并且随运行长度平方增长。"""
        store = await SqliteSessionStore.open(tmp_path / "haven.db", tmp_path / "artifacts")
        for seq in range(1, 11):
            await store.save_checkpoint(checkpoint(last_seq=seq))
        await store.save_checkpoint(checkpoint(run_id="other-run", last_seq=3))

        cursor = await store._db.execute("SELECT run_id, seq FROM checkpoints ORDER BY run_id, seq")
        rows = [(str(r["run_id"]), int(r["seq"])) for r in await cursor.fetchall()]
        assert rows == [("other-run", 3), ("run-1", 10)]
        await store.close()

    async def test_corrupted_event_fails_closed(self, tmp_path: Path) -> None:
        db, art = tmp_path / "haven.db", tmp_path / "artifacts"
        store = await SqliteSessionStore.open(db, art)
        await store.create_run("run-1", "/tmp/ws", "d", "goal", "interactive")
        await store.append_event("run-1", StepStarted(run_id="run-1", step=1))
        # 篡改日志
        await store._db.execute(
            "UPDATE events SET payload_json = ? WHERE run_id = 'run-1'",
            ('{"kind": "step.started", "run_id": "run-1", "step": 99}',),
        )
        await store._db.commit()
        with pytest.raises(StoreError, match="digest"):
            await store.load_events("run-1")
        await store.close()

    async def test_schema_version_mismatch_fails_closed(self, tmp_path: Path) -> None:
        db, art = tmp_path / "haven.db", tmp_path / "artifacts"
        with closing(sqlite3.connect(db)) as raw, raw:
            raw.execute("CREATE TABLE schema_meta (version INTEGER, migrated_at TEXT)")
            raw.execute("INSERT INTO schema_meta VALUES (999, 'future')")
        original = db.read_bytes()

        with pytest.raises(StoreError, match="schema"):
            await SqliteSessionStore.open(db, art)
        assert db.read_bytes() == original
        assert not art.exists()

    async def test_failed_migration_rolls_back_and_can_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiosqlite

        from haven.adapters.sqlite_session import DB_SCHEMA_VERSION

        db, art = tmp_path / "haven.db", tmp_path / "artifacts"
        original = await SqliteSessionStore.open(db, art)
        await original.create_run("run-1", "/ws", "ws", "old run", "interactive")
        await original.close()
        with closing(sqlite3.connect(db)) as raw:
            raw.executescript(
                "DROP TABLE project_notes; "
                "ALTER TABLE executions DROP COLUMN dest_path; "
                "UPDATE schema_meta SET version = 2;"
            )

        connect = aiosqlite.connect
        opened = []

        def tracked_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
            connection = connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with monkeypatch.context() as patch:
            patch.setattr(aiosqlite, "connect", tracked_connect)
            try:
                with pytest.raises(sqlite3.OperationalError, match="dest_path"):
                    await SqliteSessionStore.open(db, art)
                with pytest.raises(ValueError, match="no active connection"):
                    await opened[0].execute("SELECT 1")
            finally:
                for connection in opened:
                    await connection.close()

        with closing(sqlite3.connect(db)) as raw:
            assert raw.execute("SELECT version FROM schema_meta").fetchall() == [(2,)]
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_schema")}
            assert not {"executions_v3", "project_notes"} & tables
            raw.execute("ALTER TABLE executions ADD COLUMN dest_path TEXT NOT NULL DEFAULT ''")

        migrated = await SqliteSessionStore.open(db, art)
        try:
            assert await migrated.get_run("run-1") is not None
            assert await migrated.list_project_notes("/ws") == ()
            with closing(sqlite3.connect(db)) as raw:
                assert raw.execute("SELECT version FROM schema_meta").fetchall() == [
                    (DB_SCHEMA_VERSION,)
                ]
        finally:
            await migrated.close()

    @pytest.mark.parametrize("open_count", [1, 2])
    async def test_v1_database_migrates_in_place(self, tmp_path: Path, open_count: int) -> None:
        """v1 存储（没有 dest_path 列）必须能够打开、获得该列，并读回迁移前写入的
        记录。"""
        import aiosqlite

        from haven.adapters.sqlite_session import DB_SCHEMA_VERSION

        db_path, art = tmp_path / "haven.db", tmp_path / "artifacts"
        # 手动构造真实的 v1 数据库：使用 v1 的 executions 结构和 v1 meta。
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

        async def inspect_migrated() -> None:
            store = await SqliteSessionStore.open(db_path, art)
            try:
                records = await store.load_executions("run-1")
                assert records[0].dest_path == ""
                with closing(sqlite3.connect(db_path)) as migrated:
                    assert migrated.execute("SELECT version FROM schema_meta").fetchall() == [
                        (DB_SCHEMA_VERSION,)
                    ]
                    assert (
                        migrated.execute(
                            "SELECT name FROM sqlite_schema WHERE name='idx_executions_tool_name'"
                        ).fetchone()
                        is not None
                    )
            finally:
                await store.close()

        await asyncio.gather(*(inspect_migrated() for _ in range(open_count)))


async def test_sqlite_put_artifact_handles_exdev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import os

    store = await SqliteSessionStore.open(tmp_path / "haven.db", tmp_path / "artifacts")
    data = b"hello cross-device"

    called = []

    def mock_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        called.append(True)
        err = OSError()
        err.errno = errno.EXDEV
        raise err

    monkeypatch.setattr(os, "replace", mock_replace)
    try:
        digest = await store.put_artifact(data)
        assert len(digest) == 64
        assert len(called) == 1
        read_back = await store.get_artifact(digest)
        assert read_back == data
    finally:
        await store.close()


class TestBothStoresAgreeOnEdgeCases:
    """两个实现对边界与失败路径必须给出一致的答案。

    happy path 一直有参数化契约覆盖，但分歧藏在例外里——内存实现曾经在这些地方抛
    `KeyError`，甚至静默覆盖已有记录，而生产用的 SQLite 实现只是空转。这里把整条
    操作序列跑在两端并逐项比对，任何一方先改行为就会红。
    """

    @staticmethod
    async def _probe(s: SessionStorePort) -> dict[str, object]:
        out: dict[str, object] = {}
        cases: list[tuple[str, Callable[[], Awaitable[object]]]] = [
            ("update_missing_run", lambda: s.update_run_status("nope", RunStatus.SUCCEEDED, "x")),
            (
                "record_exec_unknown_run",
                lambda: s.record_execution(
                    ExecutionRecord(
                        run_id="nope",
                        call_id="c",
                        ticket_digest="t",
                        tool_name="repo.edit",
                        effect_state=EffectState.STARTED,
                        preimage_digest="p",
                        postimage_digest="q",
                        path="a.py",
                    )
                ),
            ),
            (
                "update_exec_unknown",
                lambda: s.update_execution_state("nope", "c", EffectState.CONFIRMED),
            ),
            ("record_approval_unknown_run", lambda: s.record_approval("a", "nope", "d")),
            ("consume_unknown_approval", lambda: s.consume_approval("nope", "d")),
            (
                "decide_unknown_approval",
                lambda: s.decide_approval("nope", ApprovalDecision.APPROVED),
            ),
            ("delete_missing_run", lambda: s.delete_run("nope")),
            ("clear_missing_ws_history", lambda: s.clear_input_history("nope")),
            ("clear_missing_ws_notes", lambda: s.clear_project_notes("nope")),
            ("delete_missing_artifact", lambda: s.delete_artifact("0" * 64)),
            ("empty_approval_id", lambda: s.record_approval("", "e1", "d")),
        ]
        for name, factory in cases:
            try:
                await factory()
                out[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - 比较的是异常类型本身
                out[name] = type(exc).__name__

        await s.create_run("dup", "/ws", "d1", "first", "interactive")
        try:
            await s.create_run("dup", "/ws", "d1", "second", "interactive")
            out["duplicate_create"] = "ok"
        except Exception as exc:  # noqa: BLE001
            out["duplicate_create"] = type(exc).__name__

        await s.append_event("e1", StepStarted(run_id="e1", step=1))
        await s.append_event("e1", StepStarted(run_id="e1", step=2))
        await s.append_event("e2", StepStarted(run_id="e2", step=1))
        out["seq_e1"] = [e.seq for e in await s.load_events("e1")]
        out["seq_e2"] = [e.seq for e in await s.load_events("e2")]

        for index in range(30):
            await s.record_input("cap", f"msg-{index}")
        history = await s.load_input_history("cap")
        out["input_cap_len"] = len(history)
        out["input_cap_first"] = history[0] if history else None

        try:
            for index in range(60):
                await s.add_project_note("ncap", f"n{index}")
            out["note_cap"] = len(await s.list_project_notes("ncap"))
        except Exception as exc:  # noqa: BLE001
            out["note_cap"] = f"{type(exc).__name__}:{len(await s.list_project_notes('ncap'))}"

        first = await s.put_artifact(b"same")
        second = await s.put_artifact(b"same")
        out["artifact_same_digest"] = first == second
        await s.delete_artifact(first)
        out["artifact_after_delete_one"] = await s.get_artifact(first)
        return out

    async def test_the_two_implementations_agree(self, store: SessionStorePort) -> None:
        with tempfile.TemporaryDirectory() as directory:
            other = await SqliteSessionStore.open(
                Path(directory) / "other.db", Path(directory) / "art"
            )
            try:
                expected = await self._probe(other)
            finally:
                await other.close()
        assert await self._probe(store) == expected

    async def test_a_duplicate_run_id_is_refused_by_both(self, store: SessionStorePort) -> None:
        """重复 id 必须拒绝：运行记录是审计表面，静默覆盖会让一条已有运行的事件流
        挂到另一条元数据上。"""
        await store.create_run("dup", "/ws", "d1", "first", "interactive")
        with pytest.raises(StoreError):
            await store.create_run("dup", "/ws", "d1", "second", "interactive")
        assert (await store.get_run("dup")).goal == "first"

    async def test_updating_an_unknown_run_is_a_no_op_not_a_crash(
        self, store: SessionStorePort
    ) -> None:
        """未知 run_id 安静返回：SQLite 的 `UPDATE ... WHERE id = ?` 影响 0 行，
        内存实现曾经在这里抛 `KeyError`。"""
        await store.update_run_status("nope", RunStatus.SUCCEEDED, "x")
        assert await store.get_run("nope") is None

    async def test_deciding_an_unknown_approval_is_a_no_op_not_a_crash(
        self, store: SessionStorePort
    ) -> None:
        """审批协调器拿事件里的 id 来调用；对不上时不该抛原始异常掀翻审批流程。"""
        await store.decide_approval("nope", ApprovalDecision.APPROVED)
        assert await store.consume_approval("nope", "d") is False
