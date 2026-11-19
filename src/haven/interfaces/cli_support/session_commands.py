"""运行记录的查询、重放、导出和清理命令。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from haven.interfaces.cli_support.common import EXIT_OK, EXIT_USAGE
from haven.interfaces.cli_support.sinks import ConsoleSink, _plain


def sessions_list(
    limit: int = typer.Option(20, min=0, help="Max runs to show."),
    search: str = typer.Option("", "--search", help="Search all session IDs, goals and statuses."),
) -> None:
    """列出已存储的运行，最新的排在最前。"""

    async def _list() -> None:
        from haven.bootstrap import open_store

        store = await open_store()
        try:
            runs = await store.search_runs(search, limit)
            if not runs:
                typer.echo("no runs stored yet")
                return
            for record in runs:
                typer.echo(
                    f"{record.run_id}  {record.status.value:<14} "
                    f"{record.created_at[:19]}  {record.goal[:60]}"
                )
        finally:
            await store.close()

    asyncio.run(_list())


def sessions_show(run_id: str) -> None:
    """显示一次运行已存储的事件时间线。"""

    async def _show() -> int:
        from haven.application.replay_service import ReplayService
        from haven.bootstrap import open_store

        store = await open_store()
        try:
            run = await store.get_run(run_id)
            if run is None:
                typer.echo(f"run not found: {run_id}")
                return EXIT_USAGE
            typer.echo(f"goal: {run.goal}")
            typer.echo(f"status: {run.status.value} ({run.stop_reason})")
            await ReplayService(store).replay(run_id, ConsoleSink())
            return EXIT_OK
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(_show()))


def sessions_stats(run_id: str) -> None:
    """汇总一次运行的模型、工具、重读、上下文和费用。"""

    async def _stats() -> str:
        from haven.bootstrap import open_store
        from haven.interfaces.run_stats import run_stats_text

        store = await open_store()
        try:
            return await run_stats_text(store, run_id)
        finally:
            await store.close()

    try:
        typer.echo(_plain(asyncio.run(_stats())))
    except ValueError as exc:
        typer.echo(f"error: {_plain(str(exc))}")
        raise typer.Exit(EXIT_USAGE) from exc


def gc(
    keep: int = typer.Option(20, min=0, help="Newest runs to keep regardless of age."),
    older_than_days: int | None = typer.Option(
        None,
        "--older-than-days",
        min=0,
        help="Additionally keep any run younger than this many days.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Actually delete. Default is a dry run."),
) -> None:
    """从本地存储中清理旧运行和未被引用的构件。"""

    async def _gc() -> None:
        from haven.application.maintenance import collect_garbage
        from haven.bootstrap import open_store

        store = await open_store()
        try:
            report = await collect_garbage(
                store, keep=keep, older_than_days=older_than_days, apply=yes
            )
        finally:
            await store.close()

        verb = "would delete" if report.dry_run else "deleted"
        typer.echo(
            f"{verb} {len(report.deleted)} run(s) and "
            f"{report.artifacts_deleted} unreferenced artifact(s); "
            f"keeping {len(report.kept)} run(s)"
        )
        for note in report.notes:
            typer.echo(f"note: {note}")
        for run_id in report.deleted:
            typer.echo(f"  {run_id}")
        if report.dry_run and (report.deleted or report.artifacts_deleted):
            typer.echo("re-run with --yes to apply")

    asyncio.run(_gc())


def replay(run_id: str) -> None:
    """将一次运行的日志重放到控制台（不调用模型，不执行工具）。"""

    async def _replay() -> int:
        from haven.application.replay_service import ReplayService
        from haven.bootstrap import open_store

        store = await open_store()
        try:
            envelopes = await ReplayService(store).replay(run_id, ConsoleSink())
            if not envelopes:
                typer.echo(f"no events stored for run {run_id}")
                return EXIT_USAGE
            return EXIT_OK
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(_replay()))


def export(
    run_id: str,
    fmt: str = typer.Option("markdown", "--format", help="jsonl | markdown"),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    """导出经过脱敏的运行报告。"""
    if fmt not in ("jsonl", "markdown"):
        typer.echo("error: --format must be jsonl or markdown")
        raise typer.Exit(EXIT_USAGE)

    async def _export() -> int:
        from haven.bootstrap import open_store
        from haven.interfaces.export import render_jsonl, render_markdown

        store = await open_store()
        try:
            run = await store.get_run(run_id)
            envelopes = await store.load_events(run_id)
            if run is None or not envelopes:
                typer.echo(f"run not found or empty: {run_id}")
                return EXIT_USAGE
            content = render_jsonl(envelopes) if fmt == "jsonl" else render_markdown(run, envelopes)
            if output is not None:
                # 与 `haven handoff --export`、`haven output --save` 一致：用 "x" 拒绝
                # 覆盖。静默 truncate 会让 `haven export RUN -o notes.md` 毁掉 notes.md
                # 里原有的内容，而另外两条导出路径都是明确拒绝的。
                try:
                    with output.open("x", encoding="utf-8") as handle:
                        handle.write(content)
                except FileExistsError:
                    typer.echo(f"error: {output} already exists; refusing to overwrite it")
                    return EXIT_USAGE
                typer.echo(f"wrote {output}")
            else:
                typer.echo(content)
            return EXIT_OK
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(_export()))
