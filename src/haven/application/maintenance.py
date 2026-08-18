"""存储维护：`haven gc` 背后的逻辑。

日志按设计只追加，因此磁盘使用量会随着每次运行增长，直到用户明确执行清理。清理是
用户决定的操作，绝不会自动发生：`collect_garbage` 默认只进行 dry run，保留最新运行，
从不触碰活跃运行，并且只清理没有任何存活检查点引用的构件（构件采用内容寻址，可能
在多个运行之间共享，例如两个运行归档了同一个原始文件）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from haven.domain.enums import ACTIVE_STATUSES
from haven.ports.session import SessionStorePort

#: 枚举运行时的上限，远高于任何现实中的本地日志规模。
_ALL_RUNS = 1_000_000


@dataclass(frozen=True, slots=True)
class GcReport:
    kept: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    skipped_active: tuple[str, ...] = ()
    artifacts_deleted: int = 0
    dry_run: bool = True
    notes: tuple[str, ...] = field(default=())


async def collect_garbage(
    store: SessionStorePort,
    *,
    keep: int = 20,
    older_than_days: int | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> GcReport:
    """清理旧运行和未引用的构件。

    只有当某个运行不在最新的 `keep` 个运行之内，并且（指定 `older_than_days` 时）
    早于该截止时间，才会删除它——两个条件都用于保护，而不是强制删除。活跃运行始终
    保留。`apply=False`（默认值）时不会触碰任何内容，报告只说明将会发生什么。
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")
    runs = await store.list_runs(_ALL_RUNS)  # 按最新在前排列
    cutoff = None
    if older_than_days is not None:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)

    kept: list[str] = []
    deleted: list[str] = []
    skipped_active: list[str] = []
    for index, run in enumerate(runs):
        if run.status in ACTIVE_STATUSES:
            skipped_active.append(run.run_id)
            continue
        if index < keep:
            kept.append(run.run_id)
            continue
        if cutoff is not None:
            created = _parse_timestamp(run.created_at)
            if created is None or created >= cutoff:
                kept.append(run.run_id)
                continue
        deleted.append(run.run_id)

    if apply:
        for run_id in deleted:
            await store.delete_run(run_id)

    # 清理构件：只保留仍被存活 checkpoint 引用的内容。
    # 无论是否实际删除，都基于删除后的存活集合计算，因此 dry-run 报告
    # 显示的是真实数量。
    survivors = kept + skipped_active
    referenced: set[str] = set()
    for run_id in survivors:
        checkpoint = await store.load_checkpoint(run_id)
        if checkpoint is not None:
            referenced.update(checkpoint.original_artifacts.values())
    orphaned = [digest for digest in await store.list_artifacts() if digest not in referenced]
    if apply:
        for digest in orphaned:
            await store.delete_artifact(digest)

    notes: list[str] = []
    if skipped_active:
        notes.append(f"{len(skipped_active)} active run(s) were left untouched")
    return GcReport(
        kept=tuple(kept),
        deleted=tuple(deleted),
        skipped_active=tuple(skipped_active),
        artifacts_deleted=len(orphaned),
        dry_run=not apply,
        notes=tuple(notes),
    )


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
