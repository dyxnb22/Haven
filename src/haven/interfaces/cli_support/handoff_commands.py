"""不调用模型的交接查看、准备和显式笔记修订。"""

import asyncio
from pathlib import Path

import typer

from haven.application.handoff import (
    draft_from_plan,
    render_handoff,
    render_handoff_history,
    save_handoff,
    view_handoff,
)
from haven.contracts.handoff import HandoffNotes
from haven.interfaces.cli_support.common import EXIT_USAGE
from haven.interfaces.cli_support.sinks import _plain
from haven.ports.session import ArtifactError


def handoff(
    run_id: str,
    prepare: bool = typer.Option(False, help="Prepare untrusted notes from the existing plan."),
    set_file: Path | None = typer.Option(
        None, "--set", help="Read the five note fields from JSON."
    ),
    clear: bool = typer.Option(False, help="Append a clearing revision; preserve history."),
    expected_seq: int | None = typer.Option(
        None, min=0, help="Journal sequence shown before editing."
    ),
    history: bool = typer.Option(False, help="Show the last ten note revisions."),
    export: Path | None = typer.Option(None, help="Export editable note JSON; refuse overwrite."),
) -> None:
    """查看交接事实与笔记，显式准备或修订暂停/结束的任务。"""
    modes = sum((prepare, set_file is not None, clear, history, export is not None))
    if modes > 1 or (set_file is not None and expected_seq is None):
        typer.echo(
            "error: choose one action; --set requires --expected-seq from the reviewed handoff"
        )
        raise typer.Exit(EXIT_USAGE)

    async def act() -> None:
        from haven.bootstrap import open_store

        store = await open_store()
        try:
            view = await view_handoff(store, run_id)
            if history:
                typer.echo(_plain((await render_handoff_history(store, run_id))[:30_000]))
                return
            if export is not None:
                exported_notes = view.snapshot.notes if view.snapshot else HandoffNotes()
                with export.open("x", encoding="utf-8") as output:
                    output.write(exported_notes.model_dump_json(indent=2) + "\n")
                typer.echo(
                    _plain(f"exported notes to {export}; journal sequence: {view.expected_seq}")
                )
                return
            if prepare or clear or set_file is not None:
                notes = None
                if prepare:
                    notes = draft_from_plan(view)
                elif set_file is not None:
                    with set_file.open("rb") as source:
                        raw = source.read(16_385)
                    if len(raw) > 16_384:
                        raise ValueError("handoff JSON exceeds 16 KiB")
                    notes = HandoffNotes.model_validate_json(raw)
                await save_handoff(
                    store,
                    run_id,
                    notes,
                    view.expected_seq if expected_seq is None else expected_seq,
                    source="plan_draft" if prepare else "manual",
                )
                view = await view_handoff(store, run_id)
            typer.echo(_plain(render_handoff(view)[:30_000]))
        finally:
            await store.close()

    try:
        asyncio.run(act())
    except (ValueError, OSError, ArtifactError) as exc:
        typer.echo(f"error: {_plain(str(exc))[:1000]}")
        raise typer.Exit(EXIT_USAGE) from exc
