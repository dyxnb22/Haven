"""TUI session 命令的异步应用动作，与 Textual 控件解耦。"""

from __future__ import annotations

from pathlib import Path

from haven.application.recovery_service import RecoveryService
from haven.interfaces.export import render_markdown
from haven.ports.session import SessionStorePort


async def list_sessions_text(store: SessionStorePort, limit: int = 10) -> str:
    runs = await store.list_runs(limit)
    if not runs:
        return "no recorded runs yet"
    lines = ["recent runs (use /fork RUN_ID to branch from one):"]
    lines.extend(f"  {run.run_id}  [{run.status.value}]  {run.goal[:60]}" for run in runs)
    return "\n".join(lines)


async def rewind_text(recovery: RecoveryService, run_id: str) -> str:
    report = await recovery.rewind(run_id)
    if report.blockers:
        return "rewind blocked:\n  " + "\n  ".join(report.blockers)
    parts = []
    if report.restored:
        parts.append(f"restored {len(report.restored)} file(s)")
    if report.deleted:
        parts.append(f"removed {len(report.deleted)} run-created file(s)")
    return "rewind complete: " + (", ".join(parts) or "nothing to undo")


async def export_run_text(store: SessionStorePort, run_id: str, output_dir: Path) -> str:
    run = await store.get_run(run_id)
    envelopes = await store.load_events(run_id)
    if run is None:
        return "run not found in the store"
    target = output_dir / f"haven-{run_id}.md"
    target.write_text(render_markdown(run, envelopes), encoding="utf-8")
    return f"exported to {target}"
