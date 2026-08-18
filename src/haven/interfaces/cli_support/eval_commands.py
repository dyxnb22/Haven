"""离线和在线评估命令。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from haven.config import load_config
from haven.interfaces.cli_support.common import EXIT_OK, EXIT_STOPPED, EXIT_USAGE


def eval_command(
    offline: bool = typer.Option(
        True, "--offline/--live", help="Offline uses the ScriptedModel; live calls a real provider."
    ),
    yes: bool = typer.Option(False, "--yes", help="Required for --live: confirms real spend."),
    category: str = typer.Option(
        "", "--category", help="Comma-separated categories (default: all offline / task for live)."
    ),
    cases: Path = typer.Option(Path("evals/cases"), "--cases", help="Directory of case JSON."),
    out: Path = typer.Option(Path("eval_report"), "--out", help="Report output directory."),
) -> None:
    """运行评估套件并写入 JSON + Markdown 报告。"""
    categories = tuple(c.strip() for c in category.split(",") if c.strip())

    if offline:

        async def _offline() -> int:
            from haven.evalkit.runner import run_suite

            report = await run_suite(cases_dir=cases, out_dir=out, categories=categories)
            typer.echo(report.summary_line())
            typer.echo(f"reports written to {out}/")
            return EXIT_OK if report.all_passed else EXIT_STOPPED

        raise typer.Exit(asyncio.run(_offline()))

    if not yes:
        typer.echo(
            "live eval calls a real provider for every case and will incur cost.\n"
            "re-run with --yes to confirm."
        )
        raise typer.Exit(EXIT_USAGE)

    async def _live() -> int:
        from haven.bootstrap import build_provider
        from haven.evalkit.runner import run_suite

        config = load_config(Path.cwd())
        if config.provider.api_key() is None:
            typer.echo(f"error: no API key in ${config.provider.api_key_env}")
            return EXIT_USAGE

        report = await run_suite(
            cases_dir=cases,
            out_dir=out,
            model_factory=lambda: build_provider(config),
            categories=categories or ("task",),
            report_name="report-live",
        )
        typer.echo(report.summary_line())
        typer.echo(f"model: {config.provider.model}")
        typer.echo(f"reports written to {out}/report-live.{{json,md}}")
        return EXIT_OK if report.all_passed else EXIT_STOPPED

    raise typer.Exit(asyncio.run(_live()))
