"""已保存进程结果概览、有限回看与显式原文导出。"""

import asyncio
import io
from pathlib import Path

import typer

from haven.application.process_outputs import read_process_output
from haven.interfaces.cli_support.common import EXIT_USAGE
from haven.interfaces.cli_support.sinks import _plain
from haven.interfaces.process_output import process_overview
from haven.ports.session import ArtifactError


def output(
    run_id: str,
    call_id: str = typer.Argument("", help="Omit to list checks and saved process results."),
    start_line: int = typer.Option(1, min=1),
    max_lines: int = typer.Option(200, min=1, max=1000),
    save: Path | None = typer.Option(None, help="Export the complete retained log to this file."),
) -> None:
    """查看某次运行的检查状态和进程日志；不运行模型或项目命令。"""

    async def view() -> None:
        from haven.bootstrap import open_store

        store = await open_store()
        try:
            if not call_id:
                if save is not None or start_line != 1 or max_lines != 200:
                    raise ValueError("select a CALL_ID to read or save its output")
                typer.echo(_plain(await process_overview(store, run_id)))
                return
            text = await read_process_output(store, run_id, call_id)
            if save is not None:
                # 显式路径只用于写出，不接受它来读取任意文件；避免覆盖已有资料。
                with save.open("x", encoding="utf-8") as target:
                    target.write(text)
                typer.echo(_plain(f"saved retained output to {save}"))
                return
            consumed = 0
            for number, line in enumerate(io.StringIO(text), 1):
                if number < start_line:
                    continue
                if number >= start_line + max_lines or consumed >= 20_000:
                    typer.echo(f"More output: use --start-line {number}, or --save PATH")
                    break
                fragment = line[: 20_000 - consumed]
                typer.echo(_plain(fragment), nl=not fragment.endswith("\n"))
                consumed += len(fragment)
                if len(fragment) < len(line):
                    typer.echo(
                        "Line clipped at display limit; use --save PATH for the retained log"
                    )
                    break
        finally:
            await store.close()

    try:
        asyncio.run(view())
    except (ValueError, OSError, ArtifactError) as exc:
        typer.echo(f"error: {_plain(str(exc))}")
        raise typer.Exit(EXIT_USAGE) from exc
