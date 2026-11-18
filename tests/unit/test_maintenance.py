"""`collect_garbage` 策略：保留最新运行、保护活动运行、按年龄截断、默认演练，以及
清理被引用构件。"""

from datetime import UTC, datetime

import pytest

from haven.adapters.memory_session import MemorySessionStore
from haven.application.maintenance import collect_garbage
from haven.contracts.checkpoint import (
    BudgetSnapshot,
    CheckpointV1,
    EvidenceSnapshot,
    UsageSnapshot,
)
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.enums import EffectState, RunStatus
from haven.domain.evidence import DiffEvidence, EditEvidence, EvidenceLedger
from haven.ports.session import ExecutionRecord


async def _seed_run(
    store: MemorySessionStore,
    run_id: str,
    *,
    status: RunStatus = RunStatus.SUCCEEDED,
    artifacts: dict[str, str] | None = None,
) -> None:
    await store.create_run(run_id, "/tmp/ws", "d", f"goal {run_id}", "interactive")
    await store.update_run_status(run_id, status, "done")
    await store.save_checkpoint(
        CheckpointV1(
            run_id=run_id,
            workspace_digest="ws",
            goal=f"goal {run_id}",
            mode="interactive",
            status=status.value,
            last_seq=1,
            budget=BudgetSnapshot.from_domain(Budget()),
            usage=UsageSnapshot.from_domain(BudgetUsage()),
            messages=(),
            evidence=EvidenceSnapshot(),
            original_artifacts=artifacts or {},
        )
    )


async def test_keeps_the_newest_and_deletes_the_rest() -> None:
    store = MemorySessionStore()
    for index in range(5):
        await _seed_run(store, f"run-{index}")

    report = await collect_garbage(store, keep=2, apply=True)

    # list_runs 按最新在前排列；按创建顺序，run-4 是最新的。
    assert set(report.kept) == {"run-4", "run-3"}
    assert set(report.deleted) == {"run-2", "run-1", "run-0"}
    assert await store.get_run("run-0") is None
    assert await store.get_run("run-4") is not None


async def test_dry_run_is_the_default_and_touches_nothing() -> None:
    store = MemorySessionStore()
    for index in range(3):
        await _seed_run(store, f"run-{index}")

    report = await collect_garbage(store, keep=1)

    assert report.dry_run is True
    assert len(report.deleted) == 2
    for index in range(3):
        assert await store.get_run(f"run-{index}") is not None


async def test_active_runs_are_never_deleted() -> None:
    store = MemorySessionStore()
    await _seed_run(store, "run-old-active", status=RunStatus.RUNNING_MODEL)
    await _seed_run(store, "run-new")

    report = await collect_garbage(store, keep=1, apply=True)

    assert report.skipped_active == ("run-old-active",)
    assert report.deleted == ()
    assert await store.get_run("run-old-active") is not None

    # 活跃运行不消耗“保留 N 个终态运行”的名额。
    await _seed_run(store, "run-older-terminal")
    await _seed_run(store, "run-new-active", status=RunStatus.RUNNING_MODEL)
    report = await collect_garbage(store, keep=1)
    assert "run-new-active" in report.skipped_active
    assert "run-older-terminal" in report.kept


async def test_older_than_protects_young_runs_beyond_keep() -> None:
    store = MemorySessionStore()
    for index in range(3):
        await _seed_run(store, f"run-{index}")

    # 所有内容都是“现在”创建的，因此 7 天截止时间会保护它们，即使 keep=0
    # 按其他条件本来会删除所有运行。
    report = await collect_garbage(store, keep=0, older_than_days=7, apply=True)
    assert report.deleted == ()

    # 当 `now` 远在未来时，每个运行都早于截止时间。
    future = datetime(2099, 1, 1, tzinfo=UTC)
    report = await collect_garbage(store, keep=0, older_than_days=7, apply=True, now=future)
    assert len(report.deleted) == 3


async def test_artifact_sweep_keeps_shared_references() -> None:
    store = MemorySessionStore()
    shared = await store.put_artifact(b"shared original")
    only_old = await store.put_artifact(b"old only")
    unreferenced = await store.put_artifact(b"never referenced")

    await _seed_run(store, "run-old", artifacts={"src/a.py": shared, "src/b.py": only_old})
    await _seed_run(store, "run-new", artifacts={"src/a.py": shared})

    report = await collect_garbage(store, keep=1, apply=True)

    assert report.deleted == ("run-old",)
    # shared 会保留（run-new 仍然引用它）；另外两个会被清理。
    assert await store.get_artifact(shared) == b"shared original"
    assert await store.get_artifact(only_old) is None
    assert await store.get_artifact(unreferenced) is None
    assert report.artifacts_deleted == 2


async def test_negative_keep_is_rejected() -> None:
    with pytest.raises(ValueError, match="keep"):
        await collect_garbage(MemorySessionStore(), keep=-1)
    with pytest.raises(ValueError, match="older_than_days"):
        await collect_garbage(MemorySessionStore(), older_than_days=-1)


async def test_pending_review_protects_run_and_recorded_diff_until_resolved() -> None:
    from haven.contracts.review import ReviewSnapshot

    store = MemorySessionStore()
    await _seed_run(store, "review")
    diff_digest = await store.put_artifact(b"recorded diff")
    checkpoint = await store.load_checkpoint("review")
    assert checkpoint is not None
    ledger = (
        EvidenceLedger()
        .with_edit(EditEvidence(1, "a.py", "before", "after"))
        .with_diff(DiffEvidence(2, 1, 1, 1, diff_digest))
    )
    checkpoint = checkpoint.model_copy(
        update={"require_review": True, "evidence": EvidenceSnapshot.from_domain(ledger)}
    )
    await store.save_checkpoint(checkpoint)
    report = await collect_garbage(store, keep=0, apply=True)
    assert report.skipped_active == ("review",)
    assert await store.get_artifact(diff_digest) == b"recorded diff"
    resolved = ReviewSnapshot(
        run_id="review",
        binding="binding",
        files=("a.py",),
        diff="recorded diff",
        preimages={"a.py": "before"},
        postimages={"a.py": "after"},
        applied=True,
    )
    await store.save_checkpoint(checkpoint.model_copy(update={"review": resolved}))
    report = await collect_garbage(store, keep=0, apply=True)
    assert report.deleted == ("review",)
    assert await store.get_artifact(diff_digest) is None


async def test_an_undetermined_side_effect_is_never_collected() -> None:
    """`effect_unknown` 是终态，但它的执行记录是唯一的对账证据。

    删掉运行会连带删掉这些记录，把"那次写入到底发生没有"永久变成谜。它必须和
    待审阅一样被保护，直到有人用 `haven reconcile` 做出决定。
    """
    store = MemorySessionStore()
    await _seed_run(store, "ambiguous", status=RunStatus.EFFECT_UNKNOWN)
    await store.record_execution(
        ExecutionRecord(
            run_id="ambiguous",
            call_id="call-1",
            ticket_digest="ticket",
            tool_name="repo.edit",
            effect_state=EffectState.STARTED,
            preimage_digest="before",
            postimage_digest="after",
            path="a.py",
        )
    )
    report = await collect_garbage(store, keep=0, apply=True)
    assert report.skipped_unreconciled == ("ambiguous",)
    assert report.deleted == ()
    # 记录仍在：恢复服务还能读出它并要求人类决定
    executions = await store.load_executions("ambiguous")
    assert executions and executions[0].effect_state is EffectState.STARTED
    assert any("reconcile" in note for note in report.notes)
