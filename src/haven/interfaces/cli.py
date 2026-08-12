"""Haven CLI: TUI by default, plus headless and management commands.

Stable exit codes:
0 success | 2 usage error | 3 policy/permission | 4 provider error
5 tool error | 6 budget or stopped | 7 recovery required
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from haven import __version__
from haven.application.approvals import AutoApprover
from haven.application.context_builder import MAX_CONTEXT_CHARS
from haven.config import ConfigError, explain, load_config
from haven.contracts.events import (
    ApprovalRequested,
    ContextBuilt,
    DiffPreview,
    EventEnvelope,
    ModelCompleted,
    Notice,
    PolicyDecided,
    RunCreated,
    RunFinished,
    StepStarted,
    ToolCompleted,
    ToolProposed,
)
from haven.domain.budget import BUDGET_TIERS, DEFAULT_TIER
from haven.domain.enums import PermissionMode, RunStatus, StopReason

app = typer.Typer(
    name="haven",
    help="Evidence-driven, replayable local TUI Coding Agent.",
    no_args_is_help=False,
)
sessions_app = typer.Typer(help="Inspect stored runs.")
app.add_typer(sessions_app, name="sessions")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_POLICY = 3
EXIT_PROVIDER = 4
EXIT_TOOL = 5
EXIT_STOPPED = 6
EXIT_RECOVERY = 7


def _exit_code_for(status: RunStatus, stop_reason: StopReason) -> int:
    if status is RunStatus.SUCCEEDED:
        return EXIT_OK
    if status is RunStatus.EFFECT_UNKNOWN:
        return EXIT_RECOVERY
    if stop_reason is StopReason.PROVIDER_ERROR:
        return EXIT_PROVIDER
    if stop_reason is StopReason.TOOL_ERROR:
        return EXIT_TOOL
    return EXIT_STOPPED


class ConsoleSink:
    """Compact human-readable event stream for headless runs and replay."""

    def __init__(self, verbose: bool = True) -> None:
        self._verbose = verbose

    async def emit(self, envelope: EventEnvelope) -> None:
        event = envelope.event
        line: str | None = None
        if isinstance(event, RunCreated):
            line = f"run {event.run_id} [{event.mode}] goal: {event.goal}"
        elif isinstance(event, StepStarted):
            line = f"step {event.step}"
        elif isinstance(event, ToolProposed):
            line = f"  tool {event.tool_name} {event.args_summary}"
        elif isinstance(event, PolicyDecided) and event.decision != "allow":
            line = f"  policy {event.decision} ({event.reason_code})"
        elif isinstance(event, ToolCompleted):
            state = event.status if not event.error_code else f"error:{event.error_code}"
            line = f"  -> {state} ({event.duration_ms}ms) {event.summary}"
        elif isinstance(event, ApprovalRequested):
            line = f"  approval needed: {event.summary}"
        elif isinstance(event, ModelCompleted) and event.text:
            line = f"assistant: {event.text}"
        elif isinstance(event, DiffPreview):
            line = f"  diff: {event.files_changed} file(s) +{event.insertions} -{event.deletions}"
        elif isinstance(event, Notice):
            line = f"  [{event.level}] {event.message}"
        elif isinstance(event, RunFinished):
            cached = f" cached={event.cached_input_tokens}" if event.cached_input_tokens else ""
            line = (
                f"finished: {event.status} ({event.stop_reason}) "
                f"steps={event.steps} tools={event.tool_calls} "
                f"tokens={event.input_tokens}/{event.output_tokens}{cached} "
                f"cost=${event.cost_usd:.4f}" + (" (estimated)" if event.usage_estimated else "")
            )
        if line and self._verbose:
            typer.echo(line)


class NullSink:
    async def emit(self, envelope: EventEnvelope) -> None:
        return None


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit."),
) -> None:
    """Launch the Haven TUI when called without a subcommand."""
    if version:
        typer.echo(f"haven {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        from haven.interfaces.tui.app import HavenApp

        HavenApp(workspace=Path.cwd()).run()


@app.command()
def tui(
    path: Path = typer.Argument(Path("."), help="Workspace directory."),
) -> None:
    """Launch the interactive TUI in a workspace."""
    from haven.interfaces.tui.app import HavenApp

    HavenApp(workspace=path.resolve()).run()


@app.command()
def run(
    goal: str = typer.Argument(..., help="The coding task to perform."),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    read_only: bool = typer.Option(
        True,
        "--read-only/--no-read-only",
        help="Headless runs are always read-only; disabling this is refused.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the outcome as JSON."),
    tier: str = typer.Option(
        DEFAULT_TIER,
        "--tier",
        help=(
            f"Budget preset: {', '.join(sorted(BUDGET_TIERS))}. "
            "A project file may still tighten it."
        ),
    ),
) -> None:
    """Run a goal headlessly (read-only; no approvals, no writes)."""
    if not read_only:
        typer.echo("error: headless runs are read-only by design; there is no bypass flag.")
        raise typer.Exit(EXIT_POLICY)

    async def _run() -> int:
        from haven.bootstrap import BootstrapError, build_services

        sink = NullSink() if json_output else ConsoleSink()
        try:
            services = await build_services(
                workspace,
                mode=PermissionMode.READ_ONLY,
                approvals=AutoApprover("reject_all"),
                sinks=[sink],
                tier=tier,
            )
        except (BootstrapError, ConfigError) as exc:
            typer.echo(f"error: {exc}")
            return EXIT_USAGE
        try:
            outcome = await services.run_service.run(goal)
        finally:
            await services.close()
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "run_id": outcome.run_id,
                        "status": outcome.status.value,
                        "stop_reason": outcome.stop_reason.value,
                        "steps": outcome.steps,
                        "tool_calls": outcome.tool_calls,
                        "input_tokens": outcome.input_tokens,
                        "output_tokens": outcome.output_tokens,
                        "cached_input_tokens": outcome.cached_input_tokens,
                        "cost_usd": outcome.cost_usd,
                        "usage_estimated": outcome.usage_estimated,
                        "final_text": outcome.final_text,
                    },
                    ensure_ascii=False,
                )
            )
        return _exit_code_for(outcome.status, outcome.stop_reason)

    raise typer.Exit(asyncio.run(_run()))


@app.command("continue")
def continue_(
    run_id: str = typer.Argument(..., help="The prior run to continue."),
    follow_up: str = typer.Argument(..., help="The follow-up request."),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json", help="Print the outcome as JSON."),
) -> None:
    """Ask a follow-up in the context of a prior run (read-only, headless).

    The prior transcript is carried forward, so the model answers with the
    earlier turn's context rather than from a blank slate (Phase 2).
    """

    async def _continue() -> int:
        from haven.bootstrap import BootstrapError, build_services

        sink = NullSink() if json_output else ConsoleSink()
        try:
            services = await build_services(
                workspace,
                mode=PermissionMode.READ_ONLY,
                approvals=AutoApprover("reject_all"),
                sinks=[sink],
            )
        except (BootstrapError, ConfigError) as exc:
            typer.echo(f"error: {exc}")
            return EXIT_USAGE
        try:
            outcome = await services.run_service.continue_run(run_id, follow_up)
        except ValueError as exc:
            typer.echo(f"error: {exc}")
            return EXIT_USAGE
        finally:
            await services.close()
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "run_id": outcome.run_id,
                        "parent_run_id": run_id,
                        "status": outcome.status.value,
                        "stop_reason": outcome.stop_reason.value,
                        "final_text": outcome.final_text,
                    },
                    ensure_ascii=False,
                )
            )
        return _exit_code_for(outcome.status, outcome.stop_reason)

    raise typer.Exit(asyncio.run(_continue()))


@app.command()
def doctor(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
) -> None:
    """Check the local environment without side effects or paid calls."""
    import os
    import shutil
    import sys

    failures = 0

    def check(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        mark = "ok " if ok else "FAIL"
        typer.echo(f"[{mark}] {name}: {detail}")
        if not ok:
            failures += 1

    check(
        "python",
        sys.version_info >= (3, 12),
        f"{sys.version_info.major}.{sys.version_info.minor}",
    )
    check("git", shutil.which("git") is not None, shutil.which("git") or "not found")
    ripgrep = shutil.which("rg")
    # Informational, not a failure: search falls back to a pure-Python scanner.
    typer.echo(
        f"[ok ] ripgrep: {
            ripgrep
            or 'not found — search falls back to the slower '
            'Python scanner and will not honour .gitignore'
        }"
    )
    ws = workspace.resolve()
    check("workspace", ws.is_dir(), str(ws))
    check("git repo", (ws / ".git").exists(), "found" if (ws / ".git").exists() else "not a repo")
    try:
        config = load_config(ws)
        check("config", True, "loaded")
        key = config.provider.api_key()
        check(
            "api key",
            True,
            f"{'present' if key else 'missing'} (env {config.provider.api_key_env}; "
            "only needed for live runs)",
        )
        for recipe_id, spec in sorted(config.recipes.items()):
            found = shutil.which(spec.argv[0]) is not None or Path(spec.argv[0]).exists()
            check(f"recipe {recipe_id}", found, spec.argv[0])
    except ConfigError as exc:
        check("config", False, str(exc))

    from haven.config import data_dir

    # Probe writability without creating anything: doctor must have no side
    # effects, so it inspects the nearest existing ancestor rather than running
    # mkdir. The directory is created for real on the first actual run.
    target = data_dir()
    ancestor = target
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    writable = ancestor.is_dir() and os.access(ancestor, os.W_OK)
    check(
        "data dir",
        writable,
        f"{target} ({'writable' if writable else 'not writable'}, created on first run)",
    )

    raise typer.Exit(EXIT_USAGE if failures else EXIT_OK)


@sessions_app.command("list")
def sessions_list(limit: int = typer.Option(20, help="Max runs to show.")) -> None:
    """List stored runs, newest first."""

    async def _list() -> None:
        from haven.bootstrap import open_store

        store = await open_store()
        try:
            runs = await store.list_runs(limit)
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


@sessions_app.command("show")
def sessions_show(run_id: str) -> None:
    """Show the stored event timeline of one run."""

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


@app.command()
def replay(run_id: str) -> None:
    """Replay a run's journal to the console (no model, no tools)."""

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


@app.command()
def resume(
    run_id: str,
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
) -> None:
    """Resume an interrupted run in the TUI after recovery checks."""

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


@app.command()
def reconcile(
    run_id: str,
    call_id: str,
    resolution: str = typer.Option(..., "--as", help="confirmed | not_run | abandon"),
) -> None:
    """Manually resolve an ambiguous side effect."""
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


@app.command()
def export(
    run_id: str,
    fmt: str = typer.Option("markdown", "--format", help="jsonl | markdown"),
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    """Export a redacted run report."""
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
                output.write_text(content, encoding="utf-8")
                typer.echo(f"wrote {output}")
            else:
                typer.echo(content)
            return EXIT_OK
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(_export()))


@app.command("config")
def config_explain(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    action: str = typer.Argument("explain", help="Only 'explain' is supported."),
) -> None:
    """Show every resolved config value and where it came from."""
    from haven.bootstrap import sandbox_backend_name, select_launcher

    if action != "explain":
        typer.echo("error: only `haven config explain` is supported")
        raise typer.Exit(EXIT_USAGE)
    try:
        config = load_config(workspace.resolve())
    except ConfigError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from None
    for key, value, source in explain(config, sandbox_backend_name(select_launcher())):
        typer.echo(f"{key:<32} {value:<48} [{source}]")


@app.command("debug-context")
def debug_context(
    goal: str = typer.Argument(
        "", help="Goal to preview the first-turn context for (omit when using --run)."
    ),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    run_id: str = typer.Option("", "--run", help="Show the recorded context of a stored run."),
    show_prompt: bool = typer.Option(False, "--show-prompt", help="Print the full system prompt."),
) -> None:
    """Explain what the model sees this turn, where it came from, and why.

    Makes no provider call. Without --run it previews the first turn for a goal;
    with --run it replays the context recorded at each step of a stored run.
    """
    if run_id:
        raise typer.Exit(_debug_stored_context(run_id))
    if not goal.strip():
        typer.echo("error: provide a GOAL, or --run RUN_ID to inspect a stored run")
        raise typer.Exit(EXIT_USAGE)

    async def _preview() -> int:
        from haven.bootstrap import BootstrapError, build_context_preview

        try:
            request, segments, config = await build_context_preview(workspace, goal)
        except (BootstrapError, ConfigError) as exc:
            typer.echo(f"error: {exc}")
            return EXIT_USAGE

        total = sum(seg.size_bytes for seg in segments)
        typer.echo(f"context preview for step 1 — {total} bytes across {len(segments)} segment(s)")
        typer.echo("")
        typer.echo(f"{'source':<16} {'trust':<10} {'bytes':>8}  reason")
        for segment in segments:
            typer.echo(
                f"{segment.source:<16} {segment.trust:<10} {segment.size_bytes:>8}  "
                f"{segment.reason}"
            )
        typer.echo("")
        typer.echo(
            f"tools offered ({len(request.tools)}): " + ", ".join(t.name for t in request.tools)
        )
        typer.echo(
            "check recipes: "
            + (", ".join(sorted(config.recipes)) if config.recipes else "(none registered)")
        )
        typer.echo("")
        typer.echo("NOT included, and why:")
        typer.echo("  - repository files: only what the agent reads via repo.read enters context")
        typer.echo("  - prior runs / other sessions: context is per-run, never cross-run memory")
        typer.echo("  - secrets and env: never placed in context, traces, or checkpoints")
        typer.echo(f"  - anything beyond {MAX_CONTEXT_CHARS} chars: oldest tool outputs are")
        typer.echo("    deterministically dropped first (no model-written summaries)")
        if show_prompt:
            typer.echo("")
            typer.echo("--- system prompt ---")
            typer.echo(request.messages[0].content)
        return EXIT_OK

    raise typer.Exit(asyncio.run(_preview()))


def _debug_stored_context(run_id: str) -> int:
    async def _show() -> int:
        from haven.bootstrap import open_store

        store = await open_store()
        try:
            envelopes = await store.load_events(run_id)
            if not envelopes:
                typer.echo(f"run not found or empty: {run_id}")
                return EXIT_USAGE
            built = [env.event for env in envelopes if isinstance(env.event, ContextBuilt)]
            if not built:
                typer.echo(f"run {run_id} recorded no context events")
                return EXIT_USAGE
            for event in built:
                typer.echo(f"step {event.step}: {event.total_bytes} bytes")
                for segment in event.segments:
                    typer.echo(
                        f"  {segment.source:<16} {segment.trust:<10} "
                        f"{segment.size_bytes:>8}B  {segment.reason}"
                    )
            return EXIT_OK
        finally:
            await store.close()

    return asyncio.run(_show())


@app.command("verify-provider")
def verify_provider(
    yes: bool = typer.Option(False, "--yes", help="Confirm this may incur provider cost."),
) -> None:
    """Send one tiny real model request to verify provider connectivity."""
    if not yes:
        typer.echo("this sends a real (paid) provider request; re-run with --yes to confirm")
        raise typer.Exit(EXIT_USAGE)

    async def _verify() -> int:
        import time

        from haven.bootstrap import BootstrapError, build_provider
        from haven.contracts.model import (
            ModelMessage,
            ModelRequest,
            ReasoningDelta,
            TextDelta,
            UsageReport,
        )

        config = load_config(Path.cwd())
        try:
            model = build_provider(config)
        except BootstrapError as exc:
            typer.echo(f"error: {exc}")
            return EXIT_USAGE
        started = time.monotonic()
        first_ms = 0
        chars = 0
        reasoning_chars = 0
        try:
            request = ModelRequest(
                messages=(ModelMessage(role="user", content="Reply with the word: ready"),),
                # Generous on purpose: a reasoning model spends output tokens on
                # hidden thinking before any answer text appears, so a tight cap
                # makes a healthy provider look like it returned nothing.
                max_output_tokens=512,
            )
            async for event in model.generate_stream(request):
                if first_ms == 0:
                    first_ms = int((time.monotonic() - started) * 1000)
                if isinstance(event, TextDelta):
                    chars += len(event.text)
                if isinstance(event, ReasoningDelta):
                    reasoning_chars += len(event.text)
                if isinstance(event, UsageReport):
                    typer.echo(
                        f"usage: in={event.usage.input_tokens} "
                        f"out={event.usage.output_tokens} "
                        f"(reasoning {event.usage.reasoning_tokens})"
                    )
            if chars == 0:
                typer.echo(
                    "warning: the provider returned no answer text "
                    f"(reasoning chars={reasoning_chars}); check the model name"
                )
                return EXIT_PROVIDER
            typer.echo(
                f"ok: model={config.provider.model} ttft={first_ms}ms "
                f"total={int((time.monotonic() - started) * 1000)}ms "
                f"chars={chars} reasoning_chars={reasoning_chars}"
            )
            return EXIT_OK
        except Exception as exc:  # noqa: BLE001 - report any provider failure
            typer.echo(f"provider verification failed: {exc}")
            return EXIT_PROVIDER
        finally:
            await model.aclose()

    raise typer.Exit(asyncio.run(_verify()))


@app.command("eval")
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
    """Run the eval suite and write JSON + Markdown reports.

    Offline is deterministic, free, and the CI gate. Live runs the same cases'
    goals against the real provider inside a disposable copy of each fixture —
    it costs money, is not reproducible, and never touches your own repository.
    """
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


if __name__ == "__main__":
    app()
