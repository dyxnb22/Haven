"""Presenter: a pure reducer from application events to view state.

The TUI renders PresenterState and nothing else; it never reaches into the
agent, the workspace, or the policy. Replay reuses the same reducer, which is
why a replayed run reconstructs the same screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from haven.contracts.events import (
    ApprovalDecided,
    ApprovalRequested,
    AssistantDelta,
    AssistantReasoning,
    ContextBuilt,
    DiffPreview,
    EventEnvelope,
    EvidenceRecorded,
    ModelCompleted,
    Notice,
    PlanUpdated,
    PolicyDecided,
    RunCreated,
    RunFinished,
    StepStarted,
    StreamRestarted,
    ToolCompleted,
    ToolProposed,
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize(text: str, limit: int = 2000) -> str:
    """Strip ANSI/control characters from untrusted text and bound length."""
    cleaned = _CONTROL_CHARS.sub("", text)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + " …[truncated]"
    return cleaned


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    kind: str  # user | agent | tool | policy | approval | notice | system
    text: str


@dataclass(frozen=True, slots=True)
class PresenterState:
    workspace: str = ""
    branch: str = ""
    model_name: str = ""
    mode: str = ""
    goal: str = ""
    run_id: str = ""
    status: str = "idle"
    stop_reason: str = ""
    gate_reason: str = ""
    step: int = 0
    max_steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    usage_estimated: bool = False
    running: bool = False
    streaming_text: str = ""
    reasoning_text: str = ""
    chat_text: str = ""
    diff_text: str = ""
    plan_lines: tuple[str, ...] = field(default_factory=tuple)
    context_summary: str = ""
    timeline: tuple[TimelineEntry, ...] = field(default_factory=tuple)
    evidence_rows: tuple[str, ...] = field(default_factory=tuple)
    trace_rows: tuple[str, ...] = field(default_factory=tuple)

    def header_line(self) -> str:
        parts = [
            "Haven",
            self.workspace.rsplit("/", 1)[-1] if self.workspace else "",
            self.branch,
            self.model_name,
            self.mode,
        ]
        step = f"step {self.step}/{self.max_steps}" if self.max_steps else ""
        cost = f"${self.cost_usd:.4f}" + ("~" if self.usage_estimated else "")
        return " ─ ".join(p for p in [*parts, step, cost] if p)

    def status_line(self) -> str:
        if self.running:
            return f"status: {self.status} — Ctrl+C cancels the run"
        if self.stop_reason:
            return f"finished: {self.status} ({self.stop_reason})"
        return "idle — type a task and press Enter, /help for commands"


def _push(state: PresenterState, entry: TimelineEntry) -> PresenterState:
    return replace(state, timeline=(*state.timeline, entry))


def reduce(state: PresenterState, envelope: EventEnvelope) -> PresenterState:
    event = envelope.event

    if isinstance(event, RunCreated):
        state = replace(
            state,
            run_id=event.run_id,
            goal=event.goal,
            mode=event.mode,
            workspace=event.workspace,
            branch=event.git_branch,
            model_name=event.model_name,
            max_steps=event.max_steps,
            status="running",
            stop_reason="",
            gate_reason="",
            running=True,
            chat_text="",
            diff_text="",
            streaming_text="",
            step=0,
            tool_calls=0,
            plan_lines=(),
            evidence_rows=(),
            trace_rows=(),
        )
        return _push(state, TimelineEntry("user", sanitize(event.goal)))

    if isinstance(event, StepStarted):
        return replace(state, step=event.step, status="running_model")

    if isinstance(event, AssistantDelta):
        return replace(state, streaming_text=state.streaming_text + sanitize(event.text, 10000))

    if isinstance(event, StreamRestarted):
        return replace(state, streaming_text="", reasoning_text="")

    if isinstance(event, AssistantReasoning):
        # Kept in a separate buffer so thinking is visibly distinct from the
        # answer, and cleared when the turn completes.
        return replace(state, reasoning_text=state.reasoning_text + sanitize(event.text, 10000))

    if isinstance(event, ModelCompleted):
        state = replace(
            state,
            streaming_text="",
            reasoning_text="",
            input_tokens=state.input_tokens + event.input_tokens,
            output_tokens=state.output_tokens + event.output_tokens,
            usage_estimated=state.usage_estimated or event.usage_estimated,
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
            evidence_rows=(
                *state.evidence_rows,
                f"[{event.evidence_kind}] {sanitize(event.summary, 200)}",
            ),
        )

    if isinstance(event, ContextBuilt):
        lines = [f"context for step {event.step}: {event.total_bytes} bytes"]
        lines += [
            f"  - {seg.source} ({seg.trust}, {seg.size_bytes}B): {seg.reason}"
            for seg in event.segments
        ]
        return replace(state, context_summary="\n".join(lines))

    if isinstance(event, PlanUpdated):
        marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        plan_lines = tuple(
            f"{marks.get(step.status, '[ ]')} {index}. {sanitize(step.title, 120)}"
            for index, step in enumerate(event.steps, start=1)
        )
        state = replace(state, plan_lines=plan_lines)
        done = sum(1 for step in event.steps if step.status == "done")
        return _push(state, TimelineEntry("plan", f"plan updated: {done}/{len(event.steps)} done"))

    if isinstance(event, Notice):
        return _push(
            state, TimelineEntry("notice", f"[{event.level}] {sanitize(event.message, 300)}")
        )

    if isinstance(event, RunFinished):
        state = replace(
            state,
            status=event.status,
            stop_reason=event.stop_reason,
            gate_reason=event.gate_reason,
            cost_usd=event.cost_usd,
            usage_estimated=event.usage_estimated,
            running=False,
            streaming_text="",
        )
        return _push(
            state,
            TimelineEntry(
                "system",
                f"run finished: {event.status} ({event.stop_reason}) "
                f"steps={event.steps} tools={event.tool_calls} cost=${event.cost_usd:.4f}",
            ),
        )

    return state
