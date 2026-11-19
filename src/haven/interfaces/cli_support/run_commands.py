"""无头运行与多轮继续命令。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

import typer

from haven.application.approvals import ApprovalResponder, HeadlessApprover
from haven.application.skills import SkillSelectionError
from haven.application.verification import verification_report
from haven.config import ConfigError
from haven.contracts.user_input import UserInputResponse
from haven.domain.budget import BUDGET_TIERS, DEFAULT_TIER
from haven.domain.enums import AgentMode, ApprovalMode, PermissionMode
from haven.interfaces.cli_support.common import EXIT_POLICY, EXIT_USAGE, exit_code_for
from haven.interfaces.cli_support.sinks import ConsoleSink, JsonlEventSink, NullSink
from haven.interfaces.feedback import error_hint
from haven.ports.event_sink import EventSinkPort


def _check_group_choice(name: str | None, clear: bool) -> str | None:
    if name is not None and clear:
        typer.echo("error: --check-group and --clear-check-group cannot be combined")
        raise typer.Exit(EXIT_USAGE)
    return "" if clear else name


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
    plan: bool = typer.Option(
        False,
        "--plan",
        help="Prepare a read-only plan for review; execute later with haven implement.",
    ),
    review: bool = typer.Option(
        False, "--review", help="Keep verified worktree changes for final review."
    ),
    check_group: str | None = typer.Option(
        None, "--check-group", help="Require every recipe in this configured verification group."
    ),
    model_preset: str | None = typer.Option(
        None, "--model-preset", help="Select a user model preset for this run."
    ),
) -> None:
    """无头运行一个目标。

    默认只读（提出操作但从不修改）。使用 --write 后，运行可以修改文件，但仍然
    只能通过 TUI 运行使用的同一审批 + Evidence Gate 通道；--approval-policy 提供
    相当于人类决定的策略：`reject`（只查看它会做什么）、`trusted-recipe`（执行
    验证但不修改）或 `all`（完全无人值守的自动修复）。配合 --jsonl 适合 CI。
    """
    if plan and (write or worktree or review):
        # 提示要点名真正冲突的那个：条件里含 review，只说 write/worktree 会把人指错方向。
        conflicting = [
            name
            for name, on in (("--write", write), ("--worktree", worktree), ("--review", review))
            if on
        ]
        typer.echo(f"error: --plan cannot be combined with {' or '.join(conflicting)}")
        raise typer.Exit(EXIT_USAGE)
    _run_headless(
        goal=goal,
        workspace=workspace,
        write=write,
        approval_policy=approval_policy,
        approval_mode=approval_mode,
        json_output=json_output,
        events_path=events_path,
        tier=tier,
        skills=skills,
        worktree=False if plan else worktree,
        fast_model=fast_model,
        deep_model=deep_model,
        agent_mode=AgentMode.PLAN if plan else AgentMode.AGENT,
        review_before_apply=review,
        check_group=check_group,
        model_preset=model_preset,
    )


def implement(
    run_id: str = typer.Argument(..., help="The ready planning run to confirm and execute."),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    approval_policy: str = typer.Option(
        "reject", "--approval-policy", help="Headless tool approval: reject | trusted-recipe | all."
    ),
    approval_mode: str = typer.Option("ask", "--approval-mode", help="ask | smart | full"),
    json_output: bool = typer.Option(False, "--json", "--jsonl"),
    events_path: Path | None = typer.Option(None, "--events"),
    tier: str = typer.Option(DEFAULT_TIER, "--tier"),
    skills: list[str] = typer.Option([], "--skill", help="Load a skill (repeatable)."),
    check_group: str | None = typer.Option(
        None, "--check-group", help="Select checks for this turn."
    ),
    clear_check_group: bool = typer.Option(
        False, "--clear-check-group", help="Explicitly clear inherited checks for this new turn."
    ),
    model_preset: str | None = typer.Option(
        None, "--model-preset", help="Select a user model preset for implementation."
    ),
) -> None:
    """确认并在原工作区执行已经准备好的计划，沿用工具审批和证据门禁。"""
    _run_headless(
        goal="",
        workspace=workspace,
        write=True,
        approval_policy=approval_policy,
        approval_mode=approval_mode,
        json_output=json_output,
        events_path=events_path,
        tier=tier,
        skills=skills,
        worktree=False,
        fast_model=None,
        deep_model=None,
        implement_run_id=run_id,
        check_group=_check_group_choice(check_group, clear_check_group),
        model_preset=model_preset,
    )


def answer(
    run_id: str = typer.Argument(..., help="The run waiting for your answer."),
    request_id: str = typer.Option(..., "--request", help="The pending request ID."),
    answers: str | None = typer.Option(None, "--answers", help="JSON object keyed by question ID."),
    cancel: bool = typer.Option(
        False, "--cancel", help="Cancel this task without calling the model."
    ),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    write: bool = typer.Option(False, "--write/--read-only"),
    approval_policy: str = typer.Option("reject", "--approval-policy"),
    json_output: bool = typer.Option(False, "--json", "--jsonl"),
) -> None:
    """回答已保存的问题并继续同一次运行；原有权限上限仍然生效。"""
    try:
        if not cancel and answers is None:
            raise ValueError("provide --answers JSON or --cancel")
        response = UserInputResponse.model_validate_json(
            json.dumps(
                {
                    "request_id": request_id,
                    "answers": json.loads(answers) if answers else {},
                    "action": "cancel" if cancel else "submit",
                }
            )
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from exc
    _run_headless(
        goal="",
        workspace=workspace,
        write=write,
        approval_policy=approval_policy,
        approval_mode="ask",
        json_output=json_output,
        events_path=None,
        tier=DEFAULT_TIER,
        skills=[],
        worktree=False,
        fast_model=None,
        deep_model=None,
        answer_run_id=run_id,
        response=response,
    )


def _run_headless(
    *,
    goal: str,
    workspace: Path,
    write: bool,
    approval_policy: str,
    approval_mode: str,
    json_output: bool,
    events_path: Path | None,
    tier: str,
    skills: list[str],
    worktree: bool | None,
    fast_model: str | None,
    deep_model: str | None,
    agent_mode: AgentMode = AgentMode.AGENT,
    implement_run_id: str = "",
    answer_run_id: str = "",
    response: UserInputResponse | None = None,
    review_before_apply: bool = False,
    check_group: str | None = None,
    model_preset: str | None = None,
) -> None:
    """共享无头入口的组装、事件输出、审批策略和资源清理。"""
    policy_map: dict[str, Literal["reject", "trusted_recipe", "all"]] = {
        "reject": "reject",
        "trusted-recipe": "trusted_recipe",
        "all": "all",
    }
    if approval_policy not in policy_map:
        typer.echo("error: --approval-policy must be reject, trusted-recipe, or all")
        raise typer.Exit(EXIT_USAGE)
    if model_preset is not None and (fast_model is not None or deep_model is not None):
        typer.echo("error: --model-preset cannot be combined with --fast-model or --deep-model")
        raise typer.Exit(EXIT_USAGE)

    async def _run() -> int:
        from haven.bootstrap import BootstrapError, build_services, open_store

        primary_sink = NullSink() if json_output else ConsoleSink(notify=True)
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
            resume_workspace = None
            if answer_run_id:
                lookup = await open_store()
                try:
                    saved_run = await lookup.get_run(answer_run_id)
                    if saved_run is None:
                        raise ValueError("run not found")
                    resume_workspace = Path(saved_run.workspace)
                finally:
                    await lookup.close()
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
                resume_workspace=resume_workspace,
            )
        except (BootstrapError, ConfigError, ValueError) as exc:
            if events_sink is not None:
                events_sink.close()
            typer.echo(f"error: {exc}")
            if not json_output and (hint := error_hint(str(exc))):
                typer.echo(f"Next: {hint}")
            return EXIT_USAGE
        if write and services.lease is None and services.lease_warning:
            typer.echo(f"error: {services.lease_warning}")
            await services.close()
            if events_sink is not None:
                events_sink.close()
            return EXIT_POLICY
        try:
            if review_before_apply and services.ephemeral_worktree is None:
                raise ValueError("--review requires an isolated worktree; add --worktree")
            if answer_run_id:
                assert response is not None
                outcome = await services.runtime.answer_run(answer_run_id, response)
            elif implement_run_id:
                outcome = await services.runtime.implement_plan(
                    implement_run_id,
                    skill_names=skills,
                    check_group=check_group,
                    model_preset=model_preset,
                )
            else:
                outcome = await services.runtime.start_run(
                    goal,
                    skill_names=skills,
                    agent_mode=agent_mode,
                    require_review=review_before_apply,
                    check_group=check_group,
                    model_preset=model_preset,
                )
            reconciled = await services.reconcile_worktree_if_succeeded(outcome)
            saved = await services.store.load_checkpoint(outcome.run_id)
            pending_review = (
                saved.review if saved and saved.review and not saved.review.applied else None
            )
            if reconciled and not json_output:
                typer.echo(f"Worktree reconciled {len(reconciled)} file(s) back to main workspace.")
        except ValueError as exc:
            # 选择在 turn loop 之前解析，因此一个未知名字让这次运行拒绝启动，
            # 而不是静默少一段上下文（ADR 0031）。
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
                        "cost_known": outcome.cost_known,
                        "usage_estimated": outcome.usage_estimated,
                        "final_text": outcome.final_text,
                        **(
                            {
                                "verification": verification_report(
                                    saved.verification, saved.evidence.to_domain()
                                )
                            }
                            if saved and saved.verification
                            else {}
                        ),
                        **(
                            {"review_required": True, "review_binding": pending_review.binding}
                            if pending_review
                            else {}
                        ),
                        **(
                            {"input_request": outcome.pending_input.model_dump(mode="json")}
                            if outcome.pending_input
                            else {}
                        ),
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
    write: bool = typer.Option(
        False, "--write/--read-only", help="Allow approved tools for this turn."
    ),
    approval_policy: str = typer.Option(
        "reject", "--approval-policy", help="reject | trusted-recipe | all."
    ),
    check_group: str | None = typer.Option(
        None, "--check-group", help="Select checks; omitted inherits."
    ),
    clear_check_group: bool = typer.Option(
        False, "--clear-check-group", help="Explicitly clear inherited checks for this new turn."
    ),
    model_preset: str | None = typer.Option(
        None, "--model-preset", help="Select a user model preset for this follow-up."
    ),
) -> None:
    """在之前运行的上下文中提出后续请求；默认仍为只读、无头。"""
    group = _check_group_choice(check_group, clear_check_group)
    policy_map: dict[str, Literal["reject", "trusted_recipe", "all"]] = {
        "reject": "reject",
        "trusted-recipe": "trusted_recipe",
        "all": "all",
    }
    if approval_policy not in policy_map:
        typer.echo("error: --approval-policy must be reject, trusted-recipe, or all")
        raise typer.Exit(EXIT_USAGE)

    async def _continue() -> int:
        from haven.bootstrap import BootstrapError, build_services

        sink = NullSink() if json_output else ConsoleSink(notify=True)
        try:
            services = await build_services(
                workspace,
                mode=PermissionMode.INTERACTIVE if write else PermissionMode.READ_ONLY,
                approvals=HeadlessApprover(policy_map[approval_policy]),
                sinks=[sink],
            )
        except (BootstrapError, ConfigError) as exc:
            typer.echo(f"error: {exc}")
            if not json_output and (hint := error_hint(str(exc))):
                typer.echo(f"Next: {hint}")
            return EXIT_USAGE
        if write and services.lease is None and services.lease_warning:
            typer.echo(f"error: {services.lease_warning}")
            await services.close()
            return EXIT_POLICY
        try:
            outcome = await services.run_service.continue_run(
                run_id, follow_up, skill_names=skills, check_group=group, model_preset=model_preset
            )
            saved = await services.store.load_checkpoint(outcome.run_id)
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
                        **(
                            {
                                "verification": verification_report(
                                    saved.verification, saved.evidence.to_domain()
                                )
                            }
                            if saved and saved.verification
                            else {}
                        ),
                        **(
                            {"input_request": outcome.pending_input.model_dump(mode="json")}
                            if outcome.pending_input
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        return exit_code_for(outcome.status, outcome.stop_reason)

    raise typer.Exit(asyncio.run(_continue()))
