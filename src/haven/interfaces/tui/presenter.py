"""Presenter：将应用事件纯粹归约为视图状态。

TUI 只渲染 PresenterState，不渲染其他内容；它不会深入访问代理、工作区或策略。
重放会复用同一个归约器，因此重放运行能够重建相同的界面。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from rich.markup import escape

from haven.contracts.events import (
    ApprovalDecided,
    ApprovalRequested,
    AssistantDelta,
    AssistantReasoning,
    ChildFinished,
    ChildStarted,
    ContextBuilt,
    DiffPreview,
    EventEnvelope,
    EvidenceRecorded,
    HandoffUpdated,
    ModelCompleted,
    Notice,
    PlanUpdated,
    PolicyDecided,
    ReviewRequested,
    ReviewResolved,
    RunCreated,
    RunFinished,
    SteerQueued,
    StepStarted,
    StreamRestarted,
    ToolCompleted,
    ToolProposed,
    UserInputAnswered,
    UserInputRequested,
)
from haven.contracts.model_call import ModelCallInfo
from haven.contracts.user_input import UserInputRequest
from haven.contracts.verification import VerificationSelection
from haven.domain.evidence import CheckEvidence
from haven.interfaces.model_display import cost_text, model_call_text

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize(text: str, limit: int = 2000) -> str:
    """将不可信文本处理为可安全显示的内容，并限制其长度。

    有两类风险，都来自模型或仓库控制的文本：

    - ANSI/控制序列可能重写面板周围的终端内容；
    - Rich 控制台标记会由 chat/diff/evidence/trace 面板渲染（这些是启用了标记的
      `Static` widgets）。如果原样保留，包含 `[red]`、`[/]` 或 `[link=...]` 的
      模型输出可能重新设置样式或隐藏对话记录的一部分，足以伪造令人信服的“成功”
      行。转义后，标记会以字面文本形式显示。

    时间线 `RichLog` 已经禁用标记，因此这里主要是保护各面板中的文本；统一在所有
    位置转义可以保持单一规则。
    """
    cleaned = _CONTROL_CHARS.sub("", text)
    cleaned = escape(cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + " …[truncated]"
    return cleaned


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """TUI 时间线中可展示的一条事件摘要。"""

    #: 用于选择图标的展示类别。
    kind: str  # 取值：user | agent | tool | policy | approval | notice | system
    #: 在时间线中展示的已清理文本。
    text: str


@dataclass(frozen=True, slots=True)
class PresenterState:
    """由事件折叠出的 TUI 展示状态，不拥有业务事实。"""

    #: 标题栏展示的工作区根路径。
    workspace: str = ""
    #: 当前运行记录的 Git 分支。
    branch: str = ""
    #: 配置的模型标识符。
    model_name: str = ""
    model_preset: str = ""
    last_model_call: ModelCallInfo | None = None
    #: 当前运行的权限模式。
    mode: str = ""
    #: 行为模式，规划与审批策略独立。
    agent_mode: str = "agent"
    #: 当前运行的分级审批模式（ask / smart / full）。
    approval_mode: str = "ask"
    #: 会话当前选中的技能名称（ADR 0034）。
    selected_skills: tuple[str, ...] = field(default_factory=tuple)
    #: 对话上方展示的用户目标。
    goal: str = ""
    #: 当前运行或重放运行的稳定标识。
    run_id: str = ""
    #: 标题栏展示的当前生命周期状态。
    status: str = "idle"
    #: 运行结束后的终止原因。
    stop_reason: str = ""
    #: 可用时展示的 Evidence Gate 原因。
    gate_reason: str = ""
    #: 当前已完成的模型循环轮次。
    step: int = 0
    #: 从 run.created 复制的硬轮次预算。
    max_steps: int = 0
    #: 当前已消耗的工具调用次数。
    tool_calls: int = 0
    #: 当前已消耗的输入 token 总数。
    input_tokens: int = 0
    #: 当前已消耗的输出 token 总数。
    output_tokens: int = 0
    #: 累计估算费用，单位为美元。
    cost_usd: float = 0.0
    #: 模型没有费率卡时为 False，此时数值是占位值。
    cost_known: bool = True
    #: 任意用量值来自估算时为 True。
    usage_estimated: bool = False
    #: 当前是否有正在运行的模型任务。
    running: bool = False
    #: 当前正在流式输出的临时助手可见文本。
    streaming_text: str = ""
    #: 当前正在流式输出的临时提供商推理文本。
    reasoning_text: str = ""
    #: 清理后的累计对话文本。
    chat_text: str = ""
    #: 有界的当前差异预览。
    diff_text: str = ""
    #: 渲染后的结构化计划行。
    plan_lines: tuple[str, ...] = field(default_factory=tuple)
    plan_status: str = ""
    pending_input: UserInputRequest | None = None
    review_pending: bool = False
    #: 最近一次上下文选择摘要。
    context_summary: str = ""
    #: 按顺序排列的展示时间线。
    timeline: tuple[TimelineEntry, ...] = field(default_factory=tuple)
    #: Evidence 标签页展示的证据行。
    evidence_rows: tuple[str, ...] = field(default_factory=tuple)
    process_results: tuple[ToolCompleted, ...] = ()
    #: Trace 标签页展示的追踪行。
    trace_rows: tuple[str, ...] = field(default_factory=tuple)
    verification: VerificationSelection | None = None
    verification_checks: tuple[CheckEvidence, ...] = ()
    last_edit_seq: int = -1
    next_check_group: str | None = None
    next_model_preset: str | None = None

    def header_line(self) -> str:
        """生成标题栏中的工作区、分支、模型、模式、步数和费用摘要。"""
        mode_str = (
            f"{self.mode} [{self.approval_mode}]"
            if self.mode == "interactive" and self.approval_mode
            else self.mode
        )
        if self.agent_mode == "plan":
            mode_str = f"plan ({mode_str})"
        skills_str = f"skills: [{', '.join(self.selected_skills)}]" if self.selected_skills else ""
        parts = [
            "Haven",
            self.workspace.rsplit("/", 1)[-1] if self.workspace else "",
            self.branch,
            sanitize(
                self.last_model_call.model_name if self.last_model_call else self.model_name, 100
            ),
            mode_str,
            skills_str,
        ]
        if self.model_preset:
            parts.append(f"preset: {sanitize(self.model_preset, 48)}")
        if self.next_model_preset and self.next_model_preset != self.model_preset:
            parts.append(f"next model: {sanitize(self.next_model_preset, 48)}")
        if self.verification:
            parts.append(f"checks: {sanitize(self.verification.group_name, 100)}")
        if self.next_check_group is not None and (
            self.verification is None or self.next_check_group != self.verification.group_name
        ):
            parts.append(f"next checks: {sanitize(self.next_check_group, 100) or 'none'}")
        step = f"step {self.step}/{self.max_steps}" if self.max_steps else ""
        cost = f"cost {cost_text(self.cost_usd, self.cost_known)}" if self.run_id else ""
        return " ─ ".join(p for p in [*parts, step, cost] if p)

    def status_line(self) -> str:
        """根据运行状态生成状态栏文案和取消提示。"""
        if self.pending_input is not None:
            return "waiting for your answer — /answer to reopen the question form"
        if self.review_pending:
            return "verified changes await review — /review to apply or keep isolated"
        if self.running:
            return f"status: {self.status} — Ctrl+C cancels the run"
        if self.plan_status == "ready":
            return "plan ready — /implement to execute, or send a message to revise it"
        if self.stop_reason:
            return f"finished: {self.status} ({self.stop_reason})"
        return "idle — type a task and press Enter, /help for commands"


def _push(state: PresenterState, entry: TimelineEntry) -> PresenterState:
    return replace(state, timeline=(*state.timeline, entry))


def reduce(state: PresenterState, envelope: EventEnvelope) -> PresenterState:
    """将一个事件纯归约到新的展示状态，供实时渲染和重放共用。"""
    event = envelope.event

    if isinstance(event, RunCreated):
        state = replace(
            state,
            run_id=event.run_id,
            goal=event.goal,
            mode=event.mode,
            agent_mode=event.agent_mode,
            workspace=event.workspace,
            branch=event.git_branch,
            model_name=event.model_name,
            model_preset=event.model_preset,
            last_model_call=None,
            max_steps=event.max_steps,
            status="running",
            stop_reason="",
            gate_reason="",
            running=True,
            chat_text="",
            diff_text="",
            streaming_text="",
            reasoning_text="",
            context_summary="",
            step=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            cost_known=True,
            usage_estimated=False,
            plan_lines=(),
            plan_status="",
            pending_input=None,
            review_pending=False,
            evidence_rows=(),
            process_results=(),
            trace_rows=(),
            verification=event.verification,
            verification_checks=(),
            last_edit_seq=-1,
        )
        return _push(state, TimelineEntry("user", sanitize(event.goal)))

    if isinstance(event, ChildStarted):
        return _push(
            state,
            TimelineEntry("system", f"{event.role} investigating: {sanitize(event.task, 200)}"),
        )
    if isinstance(event, ChildFinished):
        return _push(
            state,
            TimelineEntry(
                "system", f"{event.role} {event.status}: {sanitize(event.outcome_summary, 300)}"
            ),
        )
    if isinstance(event, ReviewRequested):
        return replace(state, review_pending=True)
    if isinstance(event, ReviewResolved):
        return replace(state, review_pending=False)
    if isinstance(event, UserInputRequested):
        return _push(
            replace(
                state,
                run_id=event.run_id,
                pending_input=event.request,
                status="waiting_user",
                running=False,
            ),
            TimelineEntry("notice", "waiting for your answer"),
        )

    if isinstance(event, UserInputAnswered):
        return replace(
            state,
            pending_input=None,
            running=event.response.action != "cancel",
            status="running_model" if event.response.action == "submit" else "cancelled",
        )

    if isinstance(event, StepStarted):
        return replace(state, step=event.step, status="running_model")

    if isinstance(event, AssistantDelta):
        return replace(state, streaming_text=state.streaming_text + sanitize(event.text, 10000))

    if isinstance(event, StreamRestarted):
        return replace(state, streaming_text="", reasoning_text="")

    if isinstance(event, AssistantReasoning):
        # 保存在单独的缓冲区中，使用户能明显区分思考和答案，并在轮次完成
        # 时清空。
        return replace(state, reasoning_text=state.reasoning_text + sanitize(event.text, 10000))

    if isinstance(event, ModelCompleted):
        info = event.model_info
        state = replace(
            state,
            streaming_text="",
            reasoning_text="",
            input_tokens=state.input_tokens + event.input_tokens,
            output_tokens=state.output_tokens + event.output_tokens,
            usage_estimated=state.usage_estimated or event.usage_estimated,
            last_model_call=info,
            cost_usd=state.cost_usd + (info.cost_usd if info else 0.0),
            cost_known=state.cost_known and (info.cost_known if info else False),
        )
        extra = ""
        if event.reasoning_tokens:
            extra += f" reasoning={event.reasoning_tokens}"
        if event.cached_input_tokens:
            extra += f" cached={event.cached_input_tokens}"
        state = replace(
            state,
            trace_rows=(
                *state.trace_rows,
                f"step {event.step}: model ttft={event.ttft_ms}ms "
                f"dur={event.duration_ms}ms tokens={event.input_tokens}/"
                f"{event.output_tokens}{extra} finish={event.finish_reason}",
                *((sanitize(model_call_text(info), 800),) if info else ()),
            ),
        )
        if event.text:
            text = sanitize(event.text, 8000)
            state = replace(
                state,
                chat_text=(state.chat_text + f"\n● {text}\n" if state.chat_text else f"● {text}\n"),
            )
            state = _push(state, TimelineEntry("agent", sanitize(event.text, 300)))
        return state

    if isinstance(event, HandoffUpdated):
        return _push(
            state, TimelineEntry("notice", "handoff notes revised; use /handoff to review")
        )

    if isinstance(event, ToolProposed):
        state = replace(state, tool_calls=state.tool_calls + 1, status="tool")
        return _push(
            state,
            TimelineEntry("tool", f"{event.tool_name} {sanitize(event.args_summary, 160)}"),
        )

    if isinstance(event, PolicyDecided):
        state = replace(
            state,
            trace_rows=(
                *state.trace_rows,
                f"policy {event.decision} ({event.reason_code}) risk={event.risk}",
            ),
        )
        if event.decision == "deny":
            state = _push(state, TimelineEntry("policy", f"denied: {event.reason_code}"))
        return state

    if isinstance(event, ApprovalRequested):
        state = replace(state, status="waiting_approval")
        return _push(state, TimelineEntry("approval", sanitize(event.summary, 200)))

    if isinstance(event, ApprovalDecided):
        return _push(state, TimelineEntry("approval", f"decision: {event.decision}"))

    if isinstance(event, ToolCompleted):
        outcome = event.status if not event.error_code else f"error:{event.error_code}"
        state = replace(
            state,
            process_results=(
                (*state.process_results, event)[-10:] if event.process else state.process_results
            ),
            trace_rows=(
                *state.trace_rows,
                f"tool {event.tool_name} -> {outcome} ({event.duration_ms}ms)",
            ),
        )
        return _push(
            state,
            TimelineEntry("tool", f"  ↳ {outcome} ({event.duration_ms}ms)"),
        )

    if isinstance(event, DiffPreview):
        return replace(
            state,
            diff_text=sanitize(event.preview, 100_000) or "(no changes made by this run yet)",
        )

    if isinstance(event, EvidenceRecorded):
        return replace(
            state,
            last_edit_seq=envelope.seq if event.evidence_kind == "edit" else state.last_edit_seq,
            verification_checks=(
                (*state.verification_checks, event.check.to_domain(envelope.seq))
                if event.evidence_kind == "check" and event.check
                else state.verification_checks
            ),
            evidence_rows=(
                *state.evidence_rows,
                f"[{event.evidence_kind}] {sanitize(event.summary, 200)}",
            )[-50:],
        )

    if isinstance(event, ContextBuilt):
        lines = [f"context for step {event.step}: {event.total_bytes} bytes"]
        lines += [
            f"  - {seg.source} ({seg.trust}, {seg.size_bytes}B): {seg.reason}"
            for seg in event.segments
        ]
        return replace(state, context_summary="\n".join(lines))

    if isinstance(event, PlanUpdated):
        marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]"}
        plan_lines = tuple(
            f"{marks.get(step.status, '[ ]')} {index}. {sanitize(step.title, 120)}"
            + (f" ({sanitize(step.id, 40)})" if step.id else "")
            + (
                f" — depends: {sanitize(', '.join(step.depends_on), 300)}"
                if step.depends_on
                else ""
            )
            + (
                f"\n    acceptance: {sanitize('; '.join(step.acceptance), 1500)}"
                if step.acceptance
                else ""
            )
            + (f" — blocked: {sanitize(step.blocked_reason, 300)}" if step.blocked_reason else "")
            + (f" — verified: {sanitize(', '.join(step.evidence), 300)}" if step.evidence else "")
            for index, step in enumerate(event.steps, start=1)
        )
        state = replace(state, plan_lines=plan_lines, plan_status=event.plan_status)
        done = sum(1 for step in event.steps if step.status == "done")
        return _push(state, TimelineEntry("plan", f"plan updated: {done}/{len(event.steps)} done"))

    if isinstance(event, Notice):
        return _push(
            state, TimelineEntry("notice", f"[{event.level}] {sanitize(event.message, 300)}")
        )

    if isinstance(event, SteerQueued):
        return _push(
            state, TimelineEntry("user", f"queued steering input: {sanitize(event.text, 160)}")
        )

    if isinstance(event, RunFinished):
        state = replace(
            state,
            status=event.status,
            stop_reason=event.stop_reason,
            gate_reason=event.gate_reason,
            step=event.steps,
            tool_calls=event.tool_calls,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            cost_usd=event.cost_usd,
            cost_known=event.cost_known,
            usage_estimated=event.usage_estimated,
            running=False,
            streaming_text="",
            reasoning_text="",
        )
        cost = f"cost={cost_text(event.cost_usd, event.cost_known)}"
        return _push(
            state,
            TimelineEntry(
                "system",
                f"run finished: {event.status} ({event.stop_reason}) "
                f"steps={event.steps} tools={event.tool_calls} {cost}",
            ),
        )

    return state
