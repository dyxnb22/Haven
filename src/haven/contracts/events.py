"""Typed application events: the single trace stream.

Every meaningful state transition emits one event. The TUI, the headless CLI,
the SQLite journal, replay, and export all consume the same stream, so the
interface can never diverge from the audited record.

Events are wrapped in an `EventEnvelope` which carries the sequence number
assigned at persist time. Transient events (streaming text deltas) reach the
UI but are not persisted.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from haven.contracts.base import StrictModel

SCHEMA_VERSION = 1


class RunCreated(StrictModel):
    kind: Literal["run.created"] = "run.created"
    run_id: str
    workspace: str
    workspace_digest: str
    goal: str
    mode: str
    model_name: str
    git_branch: str = ""
    git_commit: str = ""
    max_steps: int = 0


class StepStarted(StrictModel):
    kind: Literal["step.started"] = "step.started"
    run_id: str
    step: int


class AssistantDelta(StrictModel):
    """Transient streaming text; UI only, never persisted."""

    kind: Literal["assistant.delta"] = "assistant.delta"
    run_id: str
    step: int
    text: str


class AssistantReasoning(StrictModel):
    """Transient reasoning-model thinking; UI only, never persisted."""

    kind: Literal["assistant.reasoning"] = "assistant.reasoning"
    run_id: str
    step: int
    text: str


class StreamRestarted(StrictModel):
    """The turn is being retried; discard anything already shown for this step."""

    kind: Literal["stream.restarted"] = "stream.restarted"
    run_id: str
    step: int


class ModelCompleted(StrictModel):
    kind: Literal["model.completed"] = "model.completed"
    run_id: str
    step: int
    text: str
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    usage_estimated: bool
    ttft_ms: int
    duration_ms: int
    finish_reason: str
    reasoning_tokens: int = 0


class ToolProposed(StrictModel):
    kind: Literal["tool.proposed"] = "tool.proposed"
    run_id: str
    step: int
    call_id: str
    tool_name: str
    args_summary: str


class PolicyDecided(StrictModel):
    kind: Literal["policy.decided"] = "policy.decided"
    run_id: str
    call_id: str
    decision: str
    reason_code: str
    risk: str


class ApprovalRequested(StrictModel):
    kind: Literal["approval.requested"] = "approval.requested"
    run_id: str
    call_id: str
    approval_id: str
    tool_name: str
    summary: str
    preview: str
    risk: str
    request_digest: str


class ApprovalDecided(StrictModel):
    kind: Literal["approval.decided"] = "approval.decided"
    run_id: str
    approval_id: str
    decision: str


class ExecutionStarted(StrictModel):
    kind: Literal["execution.started"] = "execution.started"
    run_id: str
    call_id: str
    tool_name: str
    ticket_digest: str


class ToolCompleted(StrictModel):
    kind: Literal["tool.completed"] = "tool.completed"
    run_id: str
    call_id: str
    tool_name: str
    status: str
    error_code: str = ""
    summary: str = ""
    truncated: bool = False
    duration_ms: int = 0


class EvidenceRecorded(StrictModel):
    kind: Literal["evidence.recorded"] = "evidence.recorded"
    run_id: str
    evidence_kind: Literal["edit", "check", "diff"]
    summary: str


class DiffPreview(StrictModel):
    kind: Literal["diff.preview"] = "diff.preview"
    run_id: str
    files_changed: int
    insertions: int
    deletions: int
    preview: str


class ContextSegment(StrictModel):
    source: str
    trust: Literal["trusted", "untrusted"]
    size_bytes: int
    reason: str


class ContextBuilt(StrictModel):
    kind: Literal["context.built"] = "context.built"
    run_id: str
    step: int
    segments: tuple[ContextSegment, ...]
    total_bytes: int


class PlanStepView(StrictModel):
    title: str
    status: str


class PlanUpdated(StrictModel):
    kind: Literal["plan.updated"] = "plan.updated"
    run_id: str
    steps: tuple[PlanStepView, ...]


class Notice(StrictModel):
    kind: Literal["notice"] = "notice"
    run_id: str
    level: Literal["info", "warning", "error"]
    message: str


class EffectUnknown(StrictModel):
    kind: Literal["effect.unknown"] = "effect.unknown"
    run_id: str
    call_id: str
    tool_name: str
    detail: str


class RunFinished(StrictModel):
    kind: Literal["run.finished"] = "run.finished"
    run_id: str
    status: str
    stop_reason: str
    gate_reason: str = ""
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    usage_estimated: bool = False
    duration_ms: int = 0


ApplicationEvent = Annotated[
    RunCreated
    | StepStarted
    | AssistantDelta
    | AssistantReasoning
    | StreamRestarted
    | ModelCompleted
    | ToolProposed
    | PolicyDecided
    | ApprovalRequested
    | ApprovalDecided
    | ExecutionStarted
    | ToolCompleted
    | EvidenceRecorded
    | DiffPreview
    | ContextBuilt
    | PlanUpdated
    | Notice
    | EffectUnknown
    | RunFinished,
    Field(discriminator="kind"),
]

EVENT_ADAPTER: TypeAdapter[ApplicationEvent] = TypeAdapter(ApplicationEvent)

#: Kinds that stream to the UI but are never persisted.
TRANSIENT_KINDS = frozenset({"assistant.delta", "assistant.reasoning", "stream.restarted"})


class EventEnvelope(StrictModel):
    """An event plus its journal position. seq is 0 for transient events."""

    seq: int
    at: str
    event: ApplicationEvent
