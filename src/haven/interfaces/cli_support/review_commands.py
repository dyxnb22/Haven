"""无模型调用的隔离变更审阅与绑定合流。"""

import asyncio
import json
from pathlib import Path

import typer

from haven.application.diff_review import parse_diff
from haven.interfaces.cli_support.common import EXIT_USAGE
from haven.interfaces.cli_support.sinks import _plain
from haven.ports.workspace import WorkspaceError


def review(
    run_id: str,
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    apply: bool = typer.Option(False, "--apply"),
    discard: bool = typer.Option(False, "--discard"),
    binding: str = typer.Option(
        "", "--binding", help="The exact snapshot binding shown by review."
    ),
    file: str = typer.Option("", "--file"),
    hunk: int | None = typer.Option(None, "--hunk", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看隔离变更；--apply 或 --discard 必须绑定已审阅的快照。"""

    async def run() -> None:
        from haven.bootstrap import review_saved_run

        if (apply or discard) and (file or hunk):
            raise ValueError("file and hunk filters are for viewing; resolve the complete snapshot")
        snapshot, paths = await review_saved_run(
            run_id, workspace, binding=binding, apply=apply, discard=discard
        )
        if json_output:
            typer.echo(
                json.dumps({"review": snapshot.model_dump(mode="json"), "applied_files": paths})
            )
            return
        if apply or discard:
            typer.echo(f"{'applied' if apply else 'discarded'} review {snapshot.binding}")
            return
        view = snapshot.diff
        if file or hunk:
            files = parse_diff(view)
            selected = (
                next((item for item in files if item.path == file), None)
                if file
                else (files[0] if len(files) == 1 else None)
            )
            if selected is None:
                raise ValueError("select an existing --file")
            if hunk is not None and hunk > len(selected.hunks):
                raise ValueError("hunk does not exist")
            view = selected.hunks[hunk - 1] if hunk else selected.text
        typer.echo(_plain(view))
        typer.echo(f"Review binding: {snapshot.binding}")
        typer.echo(f"Apply with: haven review {run_id} --apply --binding {snapshot.binding}")

    try:
        asyncio.run(run())
    except (ValueError, WorkspaceError) as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from exc
