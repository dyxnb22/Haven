"""本地环境、上下文和提供商诊断命令。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from haven.application.context_builder import MAX_CONTEXT_CHARS
from haven.application.skills import SkillSelectionError, discover_skills
from haven.config import ConfigError, load_config, user_skills_dir
from haven.contracts.events import ContextBuilt
from haven.interfaces.cli_support.common import EXIT_OK, EXIT_PROVIDER, EXIT_USAGE
from haven.interfaces.cli_support.recipe_environment import (
    python_tool_dependency,
    resolve_recipe_executable,
)


def doctor(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
) -> None:
    """在没有副作用和付费调用的情况下检查本地环境。"""
    import os
    import shutil
    import sys

    from haven.bootstrap import sandbox_backend_name, select_launcher

    failures = 0

    def check(name: str, ok: bool, detail: str) -> None:
        """输出一项诊断结果，并在失败时累加退出状态。"""
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
    backend = sandbox_backend_name(select_launcher())
    if backend == "none":
        typer.echo("[info] sandbox: unavailable; repo.exec is disabled")
    else:
        check("sandbox", True, backend)
    ripgrep = shutil.which("rg")
    typer.echo(
        f"[ok ] ripgrep: {
            ripgrep
            or 'not found — search falls back to the slower '
            'Python scanner and will not honour .gitignore'
        }"
    )
    ws = workspace.resolve()
    check("workspace", ws.is_dir(), str(ws))
    is_git = (ws / ".git").exists() or any((p / ".git").exists() for p in ws.parents)
    if not is_git and shutil.which("git") is not None and ws.is_dir():
        import subprocess

        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=ws,
                capture_output=True,
                check=False,
            )
            is_git = res.returncode == 0
        except OSError:
            pass
    check("git repo", is_git, "found" if is_git else "not a repo")
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
        if config.router and config.router.enabled:
            fast = config.router.fast_model or config.provider.model
            deep = config.router.deep_model or config.provider.model
            check("router", True, f"fast={fast}, deep={deep}")
        for recipe_id, spec in sorted(config.recipes.items()):
            executable = resolve_recipe_executable(spec.argv[0], ws)
            check(
                f"recipe {recipe_id}",
                executable is not None,
                str(executable) if executable else f"{spec.argv[0]}: executable not found",
            )
            dependency = python_tool_dependency(spec, executable) if executable else None
            if dependency is not None:
                available, detail = dependency
                if available is None:
                    typer.echo(f"[info] recipe {recipe_id} dependency: {detail}")
                else:
                    check(f"recipe {recipe_id} dependency", available, detail)
        for mcp_srv in config.mcp_servers:
            mcp_found = (
                shutil.which(mcp_srv.command) is not None
                or (ws / mcp_srv.command).exists()
                or Path(mcp_srv.command).exists()
            )
            check(f"mcp {mcp_srv.name}", mcp_found, mcp_srv.command)
    except ConfigError as exc:
        check("config", False, str(exc))
    try:
        skills = discover_skills(user_root=user_skills_dir(), project_root=ws)
    except OSError as exc:
        check("skills", False, str(exc))
    else:
        # 失败即关闭必须可见：一个被拒绝的技能如果不出现在这里，用户就只会
        # 以为自己的技能放错了地方（ADR 0031）。
        check(
            "skills",
            not skills.refused,
            f"{len(skills.skills)} loaded"
            + (f", {len(skills.refused)} refused" if skills.refused else "")
            + (f" ({', '.join(skills.names())})" if skills.skills else ""),
        )
        for refusal in skills.refused:
            check(f"skill {refusal.source}", False, refusal.reason)

    from haven.config import data_dir

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


def debug_context(
    goal: str = typer.Argument(
        "", help="Goal to preview the first-turn context for (omit when using --run)."
    ),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    run_id: str = typer.Option("", "--run", help="Show the recorded context of a stored run."),
    show_prompt: bool = typer.Option(False, "--show-prompt", help="Print the full system prompt."),
    skills: list[str] = typer.Option(
        [],
        "--skill",
        help="Preview with a discovered skill loaded (repeatable). Advisory context, not tools.",
    ),
) -> None:
    """解释模型本轮看到什么、内容来自哪里以及原因是什么。"""
    if run_id:
        raise typer.Exit(_debug_stored_context(run_id))
    if not goal.strip():
        typer.echo("error: provide a GOAL, or --run RUN_ID to inspect a stored run")
        raise typer.Exit(EXIT_USAGE)

    async def _preview() -> int:
        from haven.bootstrap import BootstrapError, build_context_preview

        try:
            request, segments, config = await build_context_preview(
                workspace, goal, skill_names=skills
            )
        except (BootstrapError, ConfigError, SkillSelectionError) as exc:
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
        typer.echo("  - skill bodies: only skills you select with --skill are loaded; the")
        typer.echo("    catalog is always shown, and a skill file is not readable via repo.read")
        typer.echo(f"  - anything beyond {MAX_CONTEXT_CHARS} chars: multi-tier compaction folds")
        typer.echo("    bulky observations first, then drops oldest outputs deterministically")
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


def verify_provider(
    yes: bool = typer.Option(False, "--yes", help="Confirm this may incur provider cost."),
) -> None:
    """发送一次很小的真实模型请求，以验证提供商连通性。"""
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
        except Exception as exc:  # 报告任何提供商故障
            typer.echo(f"provider verification failed: {exc}")
            return EXIT_PROVIDER
        finally:
            closer = getattr(model, "aclose", None)
            if closer is not None:
                await closer()

    raise typer.Exit(asyncio.run(_verify()))
