"""无头运行与多轮继续命令。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

import typer

from haven.application.approvals import ApprovalResponder, AutoApprover
from haven.application.skills import SkillSelectionError
from haven.config import ConfigError
from haven.domain.budget import BUDGET_TIERS, DEFAULT_TIER
from haven.domain.enums import ApprovalMode, PermissionMode
from haven.interfaces.cli_support.common import EXIT_POLICY, EXIT_USAGE, exit_code_for
from haven.interfaces.cli_support.sinks import ConsoleSink, JsonlEventSink, NullSink
from haven.ports.event_sink import EventSinkPort


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
    approval_mode: str = typer.Option(
        "ask",
        "--approval-mode",
        help=(
            "Approval mode: ask (strict) | smart (approve for me) | "
            "full (autonomous within workspace)."
        ),
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
    skills: list[str] = typer.Option(
        [],
        "--skill",
        help=(
            "Load a discovered skill by name (repeatable). Skill bodies are advisory "
            "context, not tools. Names come from the catalog — `haven doctor` lists them."
        ),
    ),
    worktree: bool | None = typer.Option(
        None,
        "--worktree/--no-worktree",
        "--isolated/--no-isolated",
        help="Run task in an ephemeral git worktree; atomically reconcile back only on success.",
    ),
    fast_model: str | None = typer.Option(
        None,
        "--fast-model",
        help="Fast model for exploration steps (repo.list/search/outline, web.search).",
    ),
    deep_model: str | None = typer.Option(
        None,
        "--deep-model",
        help="Deep reasoning model for code mutation steps (repo.edit/create/apply_patch).",
    ),
) -> None:
    """无头运行一个目标。

    默认只读（提出操作但从不修改）。使用 --write 后，运行可以修改文件，但仍然
    只能通过 TUI 运行使用的同一审批 + Evidence Gate 通道；--approval-policy 提供
    相当于人类决定的策略：`reject`（只查看它会做什么）、`trusted-recipe`（执行
    验证但不修改）或 `all`（完全无人值守的自动修复）。配合 --jsonl 适合 CI。
    """
    policy_map: dict[str, Literal["reject", "trusted_recipe", "all"]] = {
        "reject": "reject",
        "trusted-recipe": "trusted_recipe",
        "all": "all",
    }
    if approval_policy not in policy_map:
        typer.echo("error: --approval-policy must be reject, trusted-recipe, or all")
        raise typer.Exit(EXIT_USAGE)

    async def _run() -> int:
        from haven.application.approvals import HeadlessApprover
        from haven.bootstrap import BootstrapError, build_services

        primary_sink = NullSink() if json_output else ConsoleSink()
        sinks: list[EventSinkPort] = [primary_sink]
        events_sink = JsonlEventSink(events_path) if events_path is not None else None
        if events_sink is not None:
            sinks.append(events_sink)
        if write:
            mode = PermissionMode.INTERACTIVE
            approver: ApprovalResponder = HeadlessApprover(policy_map[approval_policy])
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
                approval_mode=(
                    ApprovalMode(approval_mode)
                    if approval_mode in ApprovalMode._value2member_map_
                    else ApprovalMode.ASK
                ),
                worktree=worktree,
                fast_model=fast_model,
                deep_model=deep_model,
            )
        except (BootstrapError, ConfigError) as exc:
            if events_sink is not None:
                events_sink.close()
            typer.echo(f"error: {exc}")
            return EXIT_USAGE
        if write and services.lease is None and services.lease_warning:
            typer.echo(f"error: {services.lease_warning}")
            await services.close()
            if events_sink is not None:
                events_sink.close()
            return EXIT_POLICY
        try:
            outcome = await services.run_service.run(goal, skill_names=skills)
            reconciled = await services.reconcile_worktree_if_succeeded(outcome)
            if reconciled and not json_output:
                typer.echo(f"Worktree reconciled {len(reconciled)} file(s) back to main workspace.")
        except SkillSelectionError as exc:
            # 选择在 turn loop 之前解析，因此一个未知名字让这次运行拒绝启动，
            # 而不是静默少一段上下文（ADR 0031）。
            await services.close()
            if events_sink is not None:
                events_sink.close()
            typer.echo(f"error: {exc}")
            return EXIT_USAGE
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
        return exit_code_for(outcome.status, outcome.stop_reason)

    raise typer.Exit(asyncio.run(_run()))


def continue_(
    run_id: str = typer.Argument(..., help="The prior run to continue."),
    follow_up: str = typer.Argument(..., help="The follow-up request."),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    json_output: bool = typer.Option(False, "--json", help="Print the outcome as JSON."),
    skills: list[str] = typer.Option(
        [],
        "--skill",
        help="Load a discovered skill by name (repeatable). Advisory context, not tools.",
    ),
) -> None:
    """在之前运行的上下文中提出后续请求（只读、无头）。"""

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
            outcome = await services.run_service.continue_run(run_id, follow_up, skill_names=skills)
        except SkillSelectionError as exc:
            await services.close()
            typer.echo(f"error: {exc}")
            return EXIT_USAGE
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
        return exit_code_for(outcome.status, outcome.stop_reason)

    raise typer.Exit(asyncio.run(_continue()))
