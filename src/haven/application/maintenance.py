"""Store maintenance: the logic behind `haven gc`.

The journal is append-only by design, so disk use grows with every run until
the user prunes deliberately. Pruning is a user decision, never automatic:
`collect_garbage` defaults to a dry run, keeps the newest runs, never touches
an active run, and sweeps only artifacts that no surviving checkpoint
references (artifacts are content-addressed and may be shared between runs,
e.g. two runs that archived the same original file).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from haven.domain.enums import ACTIVE_STATUSES
from haven.ports.session import SessionStorePort

#: Upper bound when enumerating runs; far beyond any realistic local journal.
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
    """Prune old runs and unreferenced artifacts.

    A run is deleted only when it is beyond the newest `keep` runs AND (when
    `older_than_days` is given) older than that cutoff — both conditions
    protect, neither forces. Active runs are always kept. With `apply=False`
    (the default) nothing is touched and the report says what would happen.
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")
    runs = await store.list_runs(_ALL_RUNS)  # newest first
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

    # Artifact sweep: keep exactly what surviving checkpoints still reference.
    # Computed against the post-delete survivor set either way, so the dry-run
    # report shows the true count.
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
