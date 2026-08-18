"""Haven CLI：默认启动 TUI，同时提供无头和管理命令。

稳定的退出码：
0 成功 | 2 用法错误 | 3 策略/权限错误 | 4 提供商错误
5 工具错误 | 6 预算耗尽或已停止 | 7 需要恢复
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import typer

from haven import __version__
from haven.application.approvals import ApprovalResponder, AutoApprover
from haven.application.context_builder import MAX_CONTEXT_CHARS
from haven.config import ConfigError, explain, load_config
from haven.contracts.events import (
    TRANSIENT_KINDS,
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
from haven.domain.discovery import RecipeCandidate
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


#: 控制字符会在任何内容到达终端前被移除。TUI 一直这样做
#:（`presenter.sanitize`）；无头输出以前会原样回显模型控制的文本，因此模型
#: 可以发出 ANSI 转义序列来改写操作员的终端或代码托管日志行。这里不需要
#: 转义 Rich 标记——typer.echo 写入普通字节，不会渲染标记。
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _plain(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


class ConsoleSink:
    """供无头运行和重放使用的紧凑、适合人类阅读的事件流。"""

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
                # 没有费率的模型不能看起来像是免费模型。
                + (
                    f"cost=${event.cost_usd:.4f}"
                    if event.cost_known
                    else "cost=unknown (no rate card for this model)"
                )
                + (" (estimated)" if event.usage_estimated else "")
            )
        if line and self._verbose:
            typer.echo(_plain(line))


class NullSink:
    async def emit(self, envelope: EventEnvelope) -> None:
        return None


class JsonlEventSink:
    """实时将每个已持久化事件以一行 JSON 写入文件。

    这是 `--jsonl`（只打印最终结果）的 CI/自动化对应方式：通过 TUI 和日志所见的
    同一事件流，按运行发生的顺序写下整个运行过程，结果可解析且有序。只供 UI 使用
    的瞬态增量会丢弃；所有带日志 seq 的事件都会保留。
    """

    def __init__(self, path: Path) -> None:
        self._fh = path.open("w", encoding="utf-8")

    async def emit(self, envelope: EventEnvelope) -> None:
        if envelope.event.kind in TRANSIENT_KINDS:
            return
        self._fh.write(envelope.model_dump_json() + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit."),
) -> None:
    """未提供子命令时启动 Haven TUI。"""
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
    """在工作区中启动交互式 TUI。"""
    from haven.interfaces.tui.app import HavenApp

    HavenApp(workspace=path.resolve()).run()


@app.command()
def run(
    goal: str = typer.Argument(..., help="The coding task to perform."),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    write: bool = typer.Option(
        False,
        "--write/--read-only",
        help="Allow writes headlessly. Off by default; every write still goes "
        "through the approval and evidence channel under --approval-policy.",
    ),
    approval_policy: str = typer.Option(
        "reject",
        "--approval-policy",
        help="Headless approval when --write is set: reject | trusted-recipe | all.",
    ),
    json_output: bool = typer.Option(
        False, "--json", "--jsonl", help="Print the final outcome as one JSON object."
    ),
    events_path: Path | None = typer.Option(
        None,
        "--events",
        help="Stream every event as JSONL to this file, live (for CI/automation).",
    ),
    tier: str = typer.Option(
        DEFAULT_TIER,
        "--tier",
        help=(
            f"Budget preset: {', '.join(sorted(BUDGET_TIERS))}. "
            "A project file may still tighten it."
        ),
    ),
) -> None:
    """无头运行一个目标。

    默认只读（提出操作但从不修改）。使用 --write 后，运行可以修改文件，但仍然
    只能通过 TUI 运行使用的同一审批 + Evidence Gate 通道；--approval-policy 提供
    相当于人类决定的策略：`reject`（只查看它会做什么）、`trusted-recipe`（执行
    验证但不修改）或 `all`（完全无人值守的自动修复）。配合 --jsonl 适合 CI。
    """
    policy_map = {"reject": "reject", "trusted-recipe": "trusted_recipe", "all": "all"}
    if approval_policy not in policy_map:
        typer.echo("error: --approval-policy must be reject, trusted-recipe, or all")
        raise typer.Exit(EXIT_USAGE)

    async def _run() -> int:
        from haven.application.approvals import HeadlessApprover
        from haven.bootstrap import BootstrapError, build_services

        primary_sink = NullSink() if json_output else ConsoleSink()
        sinks: list[Any] = [primary_sink]
        events_sink = JsonlEventSink(events_path) if events_path is not None else None
        if events_sink is not None:
            sinks.append(events_sink)
        # 只读仍是策略层保证（无论审批者是谁都拒绝写入）；--write 会切换到
        # 交互模式，由无头审批者的策略作出决定。只有使用 --write 才能到达
        # `all`，因此无人值守的修改绝不会意外成为默认行为。
        if write:
            mode = PermissionMode.INTERACTIVE
            approver: ApprovalResponder = HeadlessApprover(policy_map[approval_policy])  # type: ignore[arg-type]
        else:
            mode = PermissionMode.READ_ONLY
            approver = HeadlessApprover("reject")
        try:
            services = await build_services(
                workspace,
                mode=mode,
                approvals=approver,
                sinks=sinks,
                tier=tier,
            )
        except (BootstrapError, ConfigError) as exc:
            if events_sink is not None:
                events_sink.close()
            typer.echo(f"error: {exc}")
            return EXIT_USAGE
        if write and services.lease is None and services.lease_warning:
            # 另一个进程持有写入租约；无头写入会静默降级为只读，因此必须告知
            # CI 调用方。
            typer.echo(f"error: {services.lease_warning}")
            await services.close()
            if events_sink is not None:
                events_sink.close()
            return EXIT_POLICY
        try:
            outcome = await services.run_service.run(goal)
        finally:
            await services.close()
            if events_sink is not None:
                events_sink.close()
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
    """在之前运行的上下文中提出后续请求（只读、无头）。

    之前的对话记录会继续传递，因此模型会基于上一轮的上下文回答，而不是从空白
    上下文开始（Phase 2）。
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
    """在没有副作用和付费调用的情况下检查本地环境。"""
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
    # 这是提示而不是失败：搜索会回退到纯 Python 扫描器。
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

    # 不创建任何内容来探测可写性：doctor 必须没有副作用，因此检查最近的
    # 已存在祖先目录，而不是运行 mkdir。目录会在第一次真正运行时创建。
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
    """列出已存储的运行，最新的排在最前。"""

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


@app.command()
def gc(
    keep: int = typer.Option(20, help="Newest runs to keep regardless of age."),
    older_than_days: int | None = typer.Option(
        None,
        "--older-than-days",
        help="Additionally keep any run younger than this many days.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Actually delete. Default is a dry run."),
) -> None:
    """从本地存储中清理旧运行和未被引用的构件。

    默认是演练：打印将要移除的内容，在传入 --yes 前不会触碰任何数据。活动运行
    始终保留；只要仍有运行的检查点引用某个构件，该构件就会继续保留。
    """

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


@app.command()
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


@app.command()
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


@app.command()
def rewind(
    run_id: str,
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
) -> None:
    """撤销已完成运行的文件变更（用户级撤回，失败即关闭）。

    运行接触过的每个文件都会恢复为运行前的内容，但前提是磁盘上的文件仍与运行
    留下的内容匹配。之后发生的任何变更（无论由你还是后续运行完成）都会阻止撤回，
    而不是被覆盖。撤回不会删除运行日志，因此历史仍可审计。
    """

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


@app.command()
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


@app.command()
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
                output.write_text(content, encoding="utf-8")
                typer.echo(f"wrote {output}")
            else:
                typer.echo(content)
            return EXIT_OK
        finally:
            await store.close()

    raise typer.Exit(asyncio.run(_export()))


@app.command()
def discover(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    accept: bool = typer.Option(
        False,
        "--accept",
        help="Persist the suggested recipes into .haven.toml (creates or appends), "
        "so they are usable on the next run without hand-editing.",
    ),
) -> None:
    """根据项目文件提出验证配方。

    读取常见项目文件（pyproject.toml、tox.ini、setup.cfg、package.json、Makefile、
    Cargo.toml、go.mod），并浅层查看 tests/ 和 src/ 布局，打印它们所暗示的
    `[recipes]` 块，使全新仓库也能获得 Evidence Gate 接受的检查命令。不会运行
    任何命令。默认只打印供审阅；使用 --accept 时会将该块写入 `.haven.toml`
    （模型仍然不会提供命令——你授权一次后，它才成为普通的已注册配方）。
    """
    from haven.domain.discovery import discover_recipes

    ws = workspace.resolve()
    files, paths = _discovery_inputs(ws)
    recipes = discover_recipes(files, paths)
    if not recipes:
        typer.echo(
            "no verification commands detected; add a [recipes] block to .haven.toml by hand"
        )
        raise typer.Exit(EXIT_OK)

    if accept:
        config_path = ws / ".haven.toml"
        added, skipped = _persist_recipes(config_path, recipes)
        for recipe_id in added:
            typer.echo(f"added [recipes.{recipe_id}] to {config_path}")
        for recipe_id in skipped:
            typer.echo(f"kept existing [recipes.{recipe_id}] (not overwritten)")
        if added:
            typer.echo("\nusable on the next run; review the file before trusting it.")
        raise typer.Exit(EXIT_OK)

    typer.echo("# Suggested recipes for .haven.toml — review, then paste what you trust:\n")
    for recipe in recipes:
        argv = ", ".join(f'"{item}"' for item in recipe.argv)
        typer.echo(f"[recipes.{recipe.id}]  # {recipe.rationale}")
        typer.echo(f"argv = [{argv}]\n")
    typer.echo("re-run with --accept to write these into .haven.toml")
    raise typer.Exit(EXIT_OK)


def _discovery_inputs(ws: Path) -> tuple[dict[str, str], list[str]]:
    """配方发现功能读取的文件内容和浅层目录列表。

    供 `discover` 和 `init` 共享，以确保二者看到完全相同的信号。只读取已知项目
    文件和一层目录，不运行任何命令。
    """
    from haven.domain.discovery import KNOWN_FILES

    files: dict[str, str] = {}
    for name in KNOWN_FILES:
        candidate = ws / name
        if candidate.is_file():
            try:
                files[name] = candidate.read_text(encoding="utf-8", errors="replace")[:65536]
            except OSError:
                continue
    # 结构信号只需要浅层列表：tests/test 目录的条目，以及任何
    # src/<pkg>/__init__.py（包初始化文件）。
    paths: list[str] = []
    for sub in ("tests", "test", "src"):
        directory = ws / sub
        if not directory.is_dir():
            continue
        try:
            for child in directory.iterdir():
                paths.append(f"{sub}/{child.name}")
                if sub == "src" and child.is_dir() and (child / "__init__.py").is_file():
                    paths.append(f"src/{child.name}/__init__.py")
        except OSError:
            continue
    return files, paths


@app.command()
def init(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    accept: bool = typer.Option(
        False, "--accept", help="Also persist the suggested recipes into .haven.toml."
    ),
) -> None:
    """一步完成初始化：环境摘要 + 配方发现。

    这是先执行 `haven doctor`、再执行 `haven discover [--accept]` 的精简组合：
    展示 Haven 将使用的内容（模型、沙箱后端、API 密钥是否存在），以及项目自身
    文件暗示的检查配方。不会运行仓库中的任何命令，写入配方仍然需要 --accept——
    无论是否接受，模型都不会提供命令。
    """
    from haven.bootstrap import sandbox_backend_name, select_launcher
    from haven.domain.discovery import discover_recipes

    ws = workspace.resolve()
    if not ws.is_dir():
        typer.echo(f"error: workspace does not exist: {ws}")
        raise typer.Exit(EXIT_USAGE)

    try:
        config = load_config(ws)
    except ConfigError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from None

    key_state = (
        "present" if config.provider.api_key() else f"missing (${config.provider.api_key_env})"
    )
    typer.echo(f"workspace:  {ws}")
    typer.echo(f"model:      {config.provider.model} @ {config.provider.base_url}")
    typer.echo(f"api key:    {key_state}")
    typer.echo(f"sandbox:    {sandbox_backend_name(select_launcher())}")
    typer.echo(f"recipes:    {len(config.recipes)} registered in .haven.toml")
    typer.echo("")

    files, paths = _discovery_inputs(ws)
    suggestions = discover_recipes(files, paths)
    fresh = [s for s in suggestions if s.id not in config.recipes]
    if not fresh:
        if config.recipes:
            typer.echo("verification is configured; nothing further to suggest.")
        else:
            typer.echo(
                "no verification commands detected; add a [recipes] block to "
                ".haven.toml by hand so the Evidence Gate has an oracle."
            )
        raise typer.Exit(EXIT_OK)

    if accept:
        added, skipped = _persist_recipes(ws / ".haven.toml", fresh)
        for recipe_id in added:
            typer.echo(f"added [recipes.{recipe_id}] to .haven.toml")
        for recipe_id in skipped:
            typer.echo(f"kept existing [recipes.{recipe_id}] (not overwritten)")
        typer.echo("\nready: `haven` opens the TUI in this workspace.")
        raise typer.Exit(EXIT_OK)

    typer.echo("suggested recipes (review, then re-run with --accept to write them):\n")
    for recipe in fresh:
        argv = ", ".join(f'"{item}"' for item in recipe.argv)
        typer.echo(f"[recipes.{recipe.id}]  # {recipe.rationale}")
        typer.echo(f"argv = [{argv}]\n")
    raise typer.Exit(EXIT_OK)


def _persist_recipes(
    config_path: Path, recipes: list[RecipeCandidate]
) -> tuple[list[str], list[str]]:
    """将发现的配方追加到 .haven.toml，绝不覆盖同 id 的现有配方。
    返回 `(新增 id 列表，跳过 id 列表)`。

    这是有意设计的最小追加器，而不是 TOML 往返解析：它只会添加新的
    `[recipes.<id>]` 表，因此不会破坏上方由用户手写的配置。已存在的 id 会原样
    保留——以用户版本为准。
    """
    existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    added: list[str] = []
    skipped: list[str] = []
    blocks: list[str] = []
    for recipe in recipes:
        if f"[recipes.{recipe.id}]" in existing:
            skipped.append(recipe.id)
            continue
        argv = ", ".join(f'"{item}"' for item in recipe.argv)
        blocks.append(f"[recipes.{recipe.id}]  # {recipe.rationale}\nargv = [{argv}]\n")
        added.append(recipe.id)
    if blocks:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        header = "" if existing else "# Written by `haven discover --accept`.\n"
        config_path.write_text(existing + prefix + header + "\n".join(blocks), encoding="utf-8")
    return added, skipped


@app.command("config")
def config_explain(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    action: str = typer.Argument("explain", help="Only 'explain' is supported."),
) -> None:
    """显示每个解析后的配置值及其来源。"""
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
    """解释模型本轮看到什么、内容来自哪里以及原因是什么。

    不会调用提供商。不使用 --run 时预览目标的第一轮；使用 --run 时重放已存储
    运行中每一步记录的上下文。
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
                # 特意设置得宽松：推理模型会在任何答案文本出现前用输出 token 进行隐藏
                # 思考，因此过紧的上限会让健康的提供商看起来像是什么都没返回。
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
        except Exception as exc:  # noqa: BLE001 - 报告任何提供商故障
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
    """运行评估套件并写入 JSON + Markdown 报告。

    离线模式是确定性的、免费的，也是 CI 门禁。实时模式会在每个夹具的临时副本
    中，将相同用例的目标交给真实提供商——会产生费用、不可复现，并且绝不会触碰
    你自己的仓库。
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
