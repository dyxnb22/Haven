"""运行恢复、撤回和副作用调和命令。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from haven.interfaces.cli_support.common import EXIT_OK, EXIT_RECOVERY, EXIT_USAGE


def resume(
    run_id: str,
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
) -> None:
    """通过恢复检查后，在 TUI 中继续一次中断的运行。"""

    async def _inspect() -> int:
        from haven.application.recovery_service import RecoveryService
        from haven.bootstrap import make_workspace, open_store

        store = await open_store()
        try:
            recovery = RecoveryService(store, make_workspace(workspace))
            report = await recovery.inspect(run_id)
            for finding in report.findings:
                typer.echo(
                    f"effect {finding.call_id} ({finding.tool_name} {finding.path}): "
                    f"{finding.classification} - {finding.detail}"
                )
            for warning in report.warnings:
                typer.echo(f"warning: {warning}")
            if not report.can_resume:
                for blocker in report.blockers:
                    typer.echo(f"blocked: {blocker}")
                typer.echo(
                    "resolve with: haven reconcile RUN_ID CALL_ID --as confirmed|not_run|abandon"
                )
                return EXIT_RECOVERY
            return EXIT_OK
        finally:
            await store.close()

    code = asyncio.run(_inspect())
    if code != EXIT_OK:
        raise typer.Exit(code)

    from haven.interfaces.tui.app import HavenApp

    HavenApp(workspace=workspace.resolve(), resume_run_id=run_id).run()


def rewind(
    run_id: str,
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
) -> None:
    """撤销已完成运行的文件变更（用户级撤回，失败即关闭）。"""

    async def _rewind() -> int:
        from haven.application.recovery_service import RecoveryService
        from haven.bootstrap import make_workspace, open_store

        store = await open_store()
        try:
            recovery = RecoveryService(store, make_workspace(workspace))
            report = await recovery.rewind(run_id)
            if not report.rewound:
                for blocker in report.blockers:
                    typer.echo(f"blocked: {blocker}")
                return EXIT_RECOVERY
            for path in report.restored:
                typer.echo(f"restored {path}")
            for path in report.deleted:
                typer.echo(f"removed  {path} (the run created it)")
            if not report.restored and not report.deleted:
                typer.echo("nothing to rewind: the run changed no files")
            return EXIT_OK
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(_rewind()))


def reconcile(
    run_id: str,
    call_id: str,
    resolution: str = typer.Option(..., "--as", help="confirmed | not_run | abandon"),
) -> None:
    """手动解决一个有歧义的副作用。"""
    if resolution not in ("confirmed", "not_run", "abandon"):
        typer.echo("error: --as must be confirmed, not_run, or abandon")
        raise typer.Exit(EXIT_USAGE)

    async def _reconcile() -> None:
        from haven.application.recovery_service import RecoveryService
        from haven.bootstrap import make_workspace, open_store

        store = await open_store()
        try:
            recovery = RecoveryService(store, make_workspace(Path.cwd()))
            await recovery.reconcile(run_id, call_id, resolution)  # type: ignore[arg-type]
            typer.echo(f"execution {call_id} marked {resolution}")
        finally:
            await store.close()

    asyncio.run(_reconcile())
