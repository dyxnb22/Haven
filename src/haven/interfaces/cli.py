"""Haven CLI entry point."""

from __future__ import annotations

import typer

from haven import __version__

app = typer.Typer(
    name="haven",
    help="Evidence-driven, replayable local TUI Coding Agent.",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit."),
) -> None:
    """Launch Haven or show help when no subcommand is given."""
    if version:
        typer.echo(f"haven {__version__}")
        raise typer.Exit()

    if ctx.invoked_subcommand is None:
        typer.echo("Haven TUI is not implemented yet. Use `haven --help` for available commands.")


@app.command()
def doctor() -> None:
    """Check local environment without side effects."""
    typer.echo("haven doctor: not implemented yet")


if __name__ == "__main__":
    app()
